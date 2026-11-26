"""Unit tests for portal claim skipping busy clients."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_claim_skips_when_client_already_running(monkeypatch) -> None:
    pytest.importorskip("sqlalchemy")
    from app.portal import store

    client_id = uuid4()
    queued = SimpleNamespace(
        id=uuid4(),
        client_id=client_id,
        status="queued",
        attempt=0,
        worker_id=None,
        started_at=None,
        updated_at=None,
        waiting_since=None,
    )

    execute_calls = {"n": 0}

    async def fake_execute(stmt, params=None):
        execute_calls["n"] += 1
        # First call: select queued job
        if execute_calls["n"] == 1:
            result = MagicMock()
            result.scalars.return_value.first.return_value = queued
            return result
        # Second: advisory lock
        return MagicMock()

    async def fake_scalar(stmt):
        return True  # client still has a running sibling

    db = AsyncMock()
    db.execute = fake_execute
    db.scalar = fake_scalar
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    claimed = await store.claim_next_queued(db, "worker-1")
    assert claimed is None
    db.rollback.assert_awaited()
    db.commit.assert_not_awaited()


def test_password_check_is_ordered_ahead_of_other_jobs() -> None:
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.dialects import postgresql

    from app.portal.store import PASSWORD_CHECK_WORKFLOW, claim_rank_for

    assert claim_rank_for(PASSWORD_CHECK_WORKFLOW, "advisor-1") == 0
    assert claim_rank_for("CHECK_ITR_VERIFICATION", "advisor-1") == 1
    assert claim_rank_for(PASSWORD_CHECK_WORKFLOW, "cron-silent") == 2
    assert claim_rank_for("CHECK_ITR_VERIFICATION", "cron") == 2

    from app.portal import store

    compiled = str(
        store.queued_claim_statement().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "CHECK_PORTAL_PASSWORD" in compiled
    assert "cron%" in compiled
