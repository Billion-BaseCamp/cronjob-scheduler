"""Weekly-list entries from checks started by hand. No database."""

from __future__ import annotations

import asyncio
import types
from uuid import uuid4

import pytest

from app.portal.notice_watchlist import ADD, REMOVE, watchlist_change


def _change(**overrides):
    values = {
        "source": "e_proceedings",
        "status": "completed",
        "error_code": None,
        "requested_by": "8f0c2f4e-advisor-sub",
        "pending_count": 2,
    }
    values.update(overrides)
    return watchlist_change(**values)


def test_manual_check_with_pending_notices_adds_the_client() -> None:
    assert _change() == ADD


def test_scheduled_check_with_pending_notices_leaves_the_list_to_the_monthly_run() -> None:
    assert _change(requested_by="cron") is None
    assert _change(requested_by="cron-silent") is None


def test_any_completed_check_with_nothing_pending_removes_the_entry() -> None:
    assert _change(pending_count=0) == REMOVE
    assert _change(pending_count=0, requested_by="cron") == REMOVE


def test_failed_checks_and_other_sources_change_nothing() -> None:
    assert _change(status="failed", error_code="UI_DRIFT") is None
    assert _change(status="waiting_for_password", error_code="MISSING_PASSWORD") is None
    assert _change(source="outstanding_demand") is None
    assert _change(source="outstanding_demand", pending_count=0) is None


class _Capture:
    def __init__(self) -> None:
        self.stmt = None

    async def execute(self, stmt) -> None:
        self.stmt = stmt


def test_add_upserts_only_the_automatic_entry() -> None:
    pytest.importorskip("nucleus.models.portal_automation.watchlist")
    from sqlalchemy.dialects import postgresql

    from app.portal import notice_watchlist

    db = _Capture()
    job = types.SimpleNamespace(id=uuid4(), client_id=uuid4(), requested_by_sub="sub")
    asyncio.run(notice_watchlist._add(db, job, 2))
    sql = str(db.stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (client_id) WHERE batch_id IS NULL AND removed_at IS NULL" in sql
    assert "DO UPDATE SET action_required_count" in sql
    assert "WHERE e_proceedings_watchlist.reason =" in sql
