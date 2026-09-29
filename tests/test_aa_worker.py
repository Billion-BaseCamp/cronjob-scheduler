"""AA worker tests against a real Postgres schema.

Retry classification and the sweeper's single-instance guard are both things
that cannot be verified by inspection: one is about which errors come back, the
other is about what happens when two processes race. Both are exercised here.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import nucleus.models  # noqa: F401
from nucleus.models.account_aggregator import (
    AAConsent,
    AACustomer,
    AAJob,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
)

from app.aa.queue import (
    claim_next_job,
    mark_failed,
    mark_succeeded,
    requeue_stale_jobs,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("AA_TEST_DATABASE_URL"),
    reason="AA_TEST_DATABASE_URL not set (needs a scratch Postgres)",
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(os.environ["AA_TEST_DATABASE_URL"])
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def consent(session_factory):
    """A fresh consent, with the job queue emptied first.

    ``claim_next_job`` deliberately claims the globally oldest due job, so
    leftovers from an earlier test would be picked up here and make assertions
    about *this* consent's job meaningless.
    """
    async with session_factory() as db:
        await db.execute(text("delete from aa_jobs"))
        await db.commit()

    async with session_factory() as db:
        client_id = uuid.uuid4()
        await db.execute(
            text(
                "insert into clients (id, first_name, last_name, is_family_member,"
                " is_advance_tax_payer) values (:i, 'W', 'Test', false, false)"
            ),
            {"i": client_id},
        )
        customer = AACustomer(
            client_id=client_id, aa_handle=f"{uuid.uuid4().hex[:10]}@finvu"
        )
        db.add(customer)
        await db.flush()
        row = AAConsent(
            aa_customer_id=customer.id, status="ACTIVE", initiated_by="client"
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


# ----------------------------------------------------------------- claiming
@pytest.mark.asyncio
async def test_claim_marks_running_and_increments_attempt(session_factory, consent):
    async with session_factory() as db:
        db.add(AAJob(job_type="FI_FETCH", aa_consent_id=consent.id, status=JOB_QUEUED))
        await db.commit()

    async with session_factory() as db:
        job = await claim_next_job(db, "worker-1")
        assert job is not None
        assert job.status == JOB_RUNNING
        assert job.attempt == 1
        assert job.worker_id == "worker-1"


@pytest.mark.asyncio
async def test_two_workers_never_claim_the_same_job(session_factory, consent):
    """The guarantee that makes it safe to run the claim loop everywhere."""
    async with session_factory() as db:
        db.add(AAJob(job_type="FI_FETCH", aa_consent_id=consent.id, status=JOB_QUEUED))
        await db.commit()

    async def claim(name):
        async with session_factory() as db:
            job = await claim_next_job(db, name)
            return job.id if job else None

    first, second = await asyncio.gather(claim("w1"), claim("w2"))
    claimed = [c for c in (first, second) if c is not None]
    assert len(claimed) == 1, "the same job was claimed twice"


@pytest.mark.asyncio
async def test_job_in_backoff_is_not_claimed(session_factory, consent):
    async with session_factory() as db:
        db.add(
            AAJob(
                job_type="FI_FETCH",
                aa_consent_id=consent.id,
                status=JOB_QUEUED,
                scheduled_for=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        )
        await db.commit()

    async with session_factory() as db:
        assert await claim_next_job(db, "w1") is None


# ------------------------------------------------------------------ retries
@pytest.mark.asyncio
async def test_terminal_error_is_not_retried(session_factory, consent):
    """A rejected consent will never succeed; retrying only hammers the vendor."""
    async with session_factory() as db:
        db.add(AAJob(job_type="FI_FETCH", aa_consent_id=consent.id, status=JOB_QUEUED))
        await db.commit()

    async with session_factory() as db:
        job = await claim_next_job(db, "w1")
        await mark_failed(
            db, job, error_code="400", error_message="consent rejected", retryable=False
        )
        assert job.status == JOB_FAILED
        assert job.finished_at is not None


@pytest.mark.asyncio
async def test_retryable_error_is_requeued_with_backoff(session_factory, consent):
    async with session_factory() as db:
        db.add(AAJob(job_type="FI_FETCH", aa_consent_id=consent.id, status=JOB_QUEUED))
        await db.commit()

    async with session_factory() as db:
        job = await claim_next_job(db, "w1")
        before = datetime.now(timezone.utc)
        await mark_failed(
            db, job, error_code="503", error_message="vendor down", retryable=True
        )
        assert job.status == JOB_QUEUED
        assert job.worker_id is None
        assert job.scheduled_for > before, "backoff was not applied"


@pytest.mark.asyncio
async def test_retry_budget_is_bounded(session_factory, consent):
    """Even retryable errors stop eventually."""
    async with session_factory() as db:
        db.add(
            AAJob(
                job_type="FI_FETCH",
                aa_consent_id=consent.id,
                status=JOB_QUEUED,
                attempt=4,
                max_attempts=5,
            )
        )
        await db.commit()

    async with session_factory() as db:
        job = await claim_next_job(db, "w1")
        assert job.attempt == 5
        await mark_failed(
            db, job, error_code="503", error_message="still down", retryable=True
        )
        assert job.status == JOB_FAILED, "exhausted job was retried anyway"


# ------------------------------------------------------------------ sweeper
@pytest.mark.asyncio
async def test_sweeper_requeues_a_dead_workers_job(session_factory, consent):
    """Without this, a crashed worker means the data silently never arrives."""
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    async with session_factory() as db:
        db.add(
            AAJob(
                job_type="FI_FETCH",
                aa_consent_id=consent.id,
                status=JOB_RUNNING,
                worker_id="dead-worker",
                attempt=1,
                heartbeat_at=stale,
                started_at=stale,
            )
        )
        await db.commit()

    async with session_factory() as db:
        count = await requeue_stale_jobs(db)
        assert count == 1

    async with session_factory() as db:
        job = await claim_next_job(db, "w-new")
        assert job is not None, "rescued job was not claimable"


@pytest.mark.asyncio
async def test_sweeper_leaves_a_live_job_alone(session_factory, consent):
    async with session_factory() as db:
        db.add(
            AAJob(
                job_type="FI_FETCH",
                aa_consent_id=consent.id,
                status=JOB_RUNNING,
                worker_id="alive",
                heartbeat_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    async with session_factory() as db:
        assert await requeue_stale_jobs(db) == 0


# ------------------------------------------------------- duplicate protection
@pytest.mark.asyncio
async def test_only_one_in_flight_job_per_consent_and_type(session_factory, consent):
    """A retried webhook must not start a competing fetch."""
    from sqlalchemy.exc import IntegrityError

    async with session_factory() as db:
        db.add(AAJob(job_type="FI_FETCH", aa_consent_id=consent.id, status=JOB_QUEUED))
        await db.commit()

    async with session_factory() as db:
        db.add(AAJob(job_type="FI_FETCH", aa_consent_id=consent.id, status=JOB_QUEUED))
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.asyncio
async def test_completed_job_does_not_block_a_new_one(session_factory, consent):
    async with session_factory() as db:
        db.add(AAJob(job_type="FI_FETCH", aa_consent_id=consent.id, status=JOB_QUEUED))
        await db.commit()

    async with session_factory() as db:
        job = await claim_next_job(db, "w1")
        await mark_succeeded(db, job)
        assert job.status == JOB_SUCCEEDED

    async with session_factory() as db:
        db.add(AAJob(job_type="FI_FETCH", aa_consent_id=consent.id, status=JOB_QUEUED))
        await db.commit()  # must not raise
