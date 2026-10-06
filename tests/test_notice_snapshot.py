"""client_notices upsert SQL. No database and no installed nucleus."""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from uuid import uuid4

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UUID as SQLUUID


class _Base(DeclarativeBase):
    pass


class _ClientNotice(_Base):
    __tablename__ = "client_notices"
    __table_args__ = (
        UniqueConstraint("client_id", "source", name="uq_client_notices_client_source"),
    )

    id: Mapped[object] = mapped_column(SQLUUID(as_uuid=True), primary_key=True)
    client_id: Mapped[object] = mapped_column(SQLUUID(as_uuid=True))
    source: Mapped[str] = mapped_column(String(32))
    notices: Mapped[list] = mapped_column(JSONB)
    notice_count: Mapped[int] = mapped_column(Integer)
    action_required_count: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    job_id: Mapped[object] = mapped_column(SQLUUID(as_uuid=True))
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str] = mapped_column(String(64))
    last_error_message: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@pytest.fixture(autouse=True)
def _client_notice_model(monkeypatch):
    """Point the lazy import at a local table; restored after each test."""
    try:
        import nucleus.models  # noqa: F401  load the real package before stubbing
    except Exception:
        pass
    module = types.ModuleType("nucleus.models.portal_automation.client_notice")
    module.ClientNotice = _ClientNotice
    monkeypatch.setitem(sys.modules, module.__name__, module)
    yield


from app.portal import notice_snapshot  # noqa: E402
from app.portal.notice_snapshot import save_notice_snapshot  # noqa: E402


class _Capture:
    def __init__(self) -> None:
        self.stmt = None

    async def execute(self, stmt) -> None:
        self.stmt = stmt


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_success_upsert_guards_with_conflict_where() -> None:
    db = _Capture()
    asyncio.run(
        save_notice_snapshot(
            db,
            client_id=uuid4(),
            source="e_proceedings",
            job_id=uuid4(),
            notices=[{"din": "1"}],
            error_code=None,
            error_message=None,
            fetched_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
        )
    )
    sql = _sql(db.stmt)
    assert "ON CONFLICT ON CONSTRAINT uq_client_notices_client_source" in sql
    assert "WHERE client_notices.fetched_at IS NULL OR client_notices.fetched_at <" in sql


def test_failure_upsert_always_updates_the_attempt() -> None:
    db = _Capture()
    asyncio.run(
        save_notice_snapshot(
            db,
            client_id=uuid4(),
            source="outstanding_demand",
            job_id=uuid4(),
            notices=None,
            error_code="INVALID_PASSWORD",
            error_message="bad password",
            fetched_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
        )
    )
    sql = _sql(db.stmt)
    assert "ON CONFLICT ON CONSTRAINT uq_client_notices_client_source" in sql
    assert "WHERE" not in sql


@pytest.fixture
def sent(monkeypatch) -> list[str]:
    subjects: list[str] = []

    async def advisor_email(db, client):
        return "advisor@example.com", "Priya"

    async def send(to_email, subject, text, html):
        subjects.append(subject)
        return True

    monkeypatch.setattr(notice_snapshot, "_advisor_email", advisor_email)
    monkeypatch.setattr(notice_snapshot, "_send", send)
    return subjects


def _job(status: str, requested_by: str = "cron"):
    return types.SimpleNamespace(
        id=uuid4(),
        client_id=uuid4(),
        status=status,
        requested_by_sub=requested_by,
        started_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
        result={},
    )


_CLIENT = types.SimpleNamespace(first_name="Rahul", last_name="Sharma", pan_number="ABCDE1234F")
_PENDING = [{"source": "e_proceedings", "has_submit_response": True, "din": "1"}]


def test_pending_notices_send_the_action_required_email(sent) -> None:
    job = _job("completed")
    asyncio.run(
        notice_snapshot.record_notice_outcome(
            _Capture(), job, _CLIENT, "e_proceedings", notices=_PENDING
        )
    )
    assert len(sent) == 1
    assert sent[0].startswith("Action required: Rahul Sharma")
    assert "alert_emailed_at" in job.result


@pytest.mark.parametrize(
    "status, code",
    [
        ("failed", "INVALID_PASSWORD"),
        ("waiting_for_password", "MISSING_PASSWORD"),
        ("waiting_for_otp", "OTP_REQUIRED"),
        ("failed", "DUAL_LOGIN"),
        ("failed", "UI_DRIFT"),
    ],
)
def test_login_and_technical_failures_send_no_email(sent, status, code) -> None:
    asyncio.run(
        notice_snapshot.record_notice_outcome(
            _Capture(), _job(status), _CLIENT, "e_proceedings",
            error_code=code, error_message="x",
        )
    )
    assert sent == []


def test_no_email_without_pending_notices_or_outside_cron(sent) -> None:
    done = [{"source": "e_proceedings", "has_submit_response": False, "din": "2"}]
    asyncio.run(
        notice_snapshot.record_notice_outcome(
            _Capture(), _job("completed"), _CLIENT, "e_proceedings", notices=done
        )
    )
    asyncio.run(
        notice_snapshot.record_notice_outcome(
            _Capture(), _job("completed", "cron-silent"), _CLIENT, "e_proceedings",
            notices=_PENDING,
        )
    )
    assert sent == []
