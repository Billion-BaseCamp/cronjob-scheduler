"""Weekly-list rules: who adds, who removes. No database."""

from __future__ import annotations

import asyncio
import types
from uuid import uuid4

import pytest

from app.portal import notice_watchlist
from app.portal.notice_watchlist import ADD, REMOVE, watchlist_change


def _change(**overrides):
    values = {
        "source": "e_proceedings",
        "status": "completed",
        "error_code": None,
        "pending_count": 2,
        "monthly": False,
    }
    values.update(overrides)
    return watchlist_change(**values)


def test_any_completed_check_with_pending_notices_adds_the_client() -> None:
    assert _change() == ADD
    assert _change(monthly=True) == ADD


def test_only_a_monthly_check_with_nothing_pending_removes() -> None:
    assert _change(pending_count=0, monthly=True) == REMOVE
    assert _change(pending_count=0, monthly=False) is None


def test_failed_checks_and_other_sources_change_nothing() -> None:
    assert _change(status="failed", error_code="UI_DRIFT") is None
    assert _change(status="failed", error_code="UI_DRIFT", monthly=True, pending_count=0) is None
    assert _change(status="waiting_for_password", error_code="MISSING_PASSWORD") is None
    assert _change(source="outstanding_demand") is None
    assert _change(source="outstanding_demand", pending_count=0, monthly=True) is None


class _Capture:
    def __init__(self) -> None:
        self.stmt = None

    async def execute(self, stmt) -> None:
        self.stmt = stmt


def _sql(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect()))


def test_add_upserts_on_the_one_active_row_per_client() -> None:
    pytest.importorskip("nucleus.models.portal_automation.watchlist")
    db = _Capture()
    job = types.SimpleNamespace(id=uuid4(), client_id=uuid4(), requested_by_sub="sub")
    asyncio.run(notice_watchlist._add(db, job))
    sql = _sql(db.stmt)
    assert "ON CONFLICT (client_id) WHERE removed_at IS NULL" in sql
    assert "DO UPDATE SET job_id" in sql


def test_remove_never_touches_manual_rows() -> None:
    pytest.importorskip("nucleus.models.portal_automation.watchlist")
    from datetime import datetime, timezone

    db = _Capture()
    job = types.SimpleNamespace(id=uuid4(), client_id=uuid4())
    asyncio.run(notice_watchlist._remove(db, job, datetime.now(timezone.utc)))
    sql = _sql(db.stmt)
    assert "e_proceedings_watchlist.removed_at IS NULL" in sql
    assert "e_proceedings_watchlist.reason =" in sql


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Db:
    def begin_nested(self):
        return _Nested()


@pytest.fixture
def calls(monkeypatch) -> list[str]:
    seen: list[str] = []

    async def add(db, job):
        seen.append("add")

    async def remove(db, job, now):
        seen.append("remove")

    monkeypatch.setattr(notice_watchlist, "_add", add)
    monkeypatch.setattr(notice_watchlist, "_remove", remove)
    return seen


@pytest.fixture
def sync(monkeypatch):
    def run(job, notices, *, monthly: bool, error_code=None):
        async def is_monthly(db, job):
            return monthly

        monkeypatch.setattr(notice_watchlist, "_is_monthly", is_monthly)
        return asyncio.run(
            notice_watchlist.sync_watchlist(
                _Db(), job, "e_proceedings", notices=notices, error_code=error_code
            )
        )

    return run


_PENDING = [
    {"source": "e_proceedings", "has_submit_response": True, "response_due_date": "2026-10-20"}
]


def _job(status: str = "completed"):
    return types.SimpleNamespace(id=uuid4(), client_id=uuid4(), status=status)


def test_weekly_or_hand_run_clean_result_keeps_the_client(sync, calls) -> None:
    assert sync(_job(), [], monthly=False) is None
    assert calls == []


def test_monthly_clean_result_removes(sync, calls) -> None:
    assert sync(_job(), [], monthly=True) == REMOVE
    assert calls == ["remove"]


def test_pending_notice_adds_whatever_started_the_job(sync, calls) -> None:
    assert sync(_job(), _PENDING, monthly=False) == ADD
    assert sync(_job(), _PENDING, monthly=True) == ADD
    assert calls == ["add", "add"]


def test_failed_job_changes_nothing(sync, calls) -> None:
    assert sync(_job("failed"), None, monthly=True, error_code="UI_DRIFT") is None
    assert calls == []


def test_is_monthly_reads_the_batch_run_type() -> None:
    pytest.importorskip("nucleus.models.portal_automation.watchlist")

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _BatchDb:
        def __init__(self, run_type):
            self.run_type = run_type
            self.stmt = None

        async def execute(self, stmt):
            self.stmt = stmt
            return _Result(self.run_type)

    no_batch = types.SimpleNamespace(batch_id=None)
    assert asyncio.run(notice_watchlist._is_monthly(_BatchDb(None), no_batch)) is False

    job = types.SimpleNamespace(batch_id=uuid4())
    monthly_db = _BatchDb("e_proceedings_monthly")
    assert asyncio.run(notice_watchlist._is_monthly(monthly_db, job)) is True
    assert "portal_automation_batches.run_type" in _sql(monthly_db.stmt)
    weekly_db = _BatchDb("e_proceedings_weekly")
    assert asyncio.run(notice_watchlist._is_monthly(weekly_db, job)) is False
