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


def _install_model() -> None:
    for name in (
        "nucleus",
        "nucleus.models",
        "nucleus.models.portal_automation",
        "nucleus.models.portal_automation.client_notice",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["nucleus.models.portal_automation.client_notice"].ClientNotice = _ClientNotice


_install_model()

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
