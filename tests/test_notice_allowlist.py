"""NOTICE_CRON_CLIENT_IDS limits scheduled notice runs on staging."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from app.core.config import parse_uuid_list, settings

A = UUID("11111111-1111-1111-1111-111111111111")
B = UUID("22222222-2222-2222-2222-222222222222")


def test_blank_means_no_limit() -> None:
    assert parse_uuid_list(None) is None
    assert parse_uuid_list("") is None
    assert parse_uuid_list("   ") is None


def test_parses_trims_and_dedupes() -> None:
    assert parse_uuid_list(f" {A} ,{B},{A}") == (A, B)


def test_malformed_entries_are_dropped() -> None:
    assert parse_uuid_list(f"{A},not-a-uuid,") == (A,)


def test_only_typos_checks_nobody_instead_of_everyone() -> None:
    assert parse_uuid_list("not-a-uuid") == ()


class _Result:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def first(self):
        return self._values[0] if self._values else None


class _FakeDb:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Result(self._responses.pop(0) if self._responses else [])


def _sql(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect()))


@pytest.fixture
def cron():
    pytest.importorskip("nucleus.models.portal_automation.watchlist")
    from app.portal import notice_cron

    return notice_cron


def test_monthly_clients_unlimited_without_allowlist(cron, monkeypatch) -> None:
    monkeypatch.setattr(settings, "NOTICE_CRON_CLIENT_IDS", None)
    client_model = cron._models()[0]
    db = _FakeDb()
    asyncio.run(cron._all_eligible_clients(db, client_model))
    assert "clients.id IN" not in _sql(db.statements[0])
    assert cron._scope_summary() == {"scope": "all"}


def test_monthly_clients_limited_by_allowlist(cron, monkeypatch) -> None:
    monkeypatch.setattr(settings, "NOTICE_CRON_CLIENT_IDS", (A, B))
    client_model = cron._models()[0]
    db = _FakeDb()
    asyncio.run(cron._all_eligible_clients(db, client_model))
    assert "clients.id IN" in _sql(db.statements[0])
    assert cron._scope_summary() == {"scope": "allowlist", "allowlist_size": 2}


def test_weekly_clients_limited_by_allowlist(cron, monkeypatch) -> None:
    client_model, watch_model, batch_model, _ = cron._models()
    manual_client = uuid4()

    def final_query(allowlist):
        monkeypatch.setattr(settings, "NOTICE_CRON_CLIENT_IDS", allowlist)
        # latest monthly batch: none; manual watchlist rows: one client
        db = _FakeDb([], [manual_client])
        asyncio.run(cron._weekly_clients(db, client_model, watch_model, batch_model))
        return _sql(db.statements[-1])

    assert final_query(None).count("clients.id IN") == 1
    assert final_query((A,)).count("clients.id IN") == 2
