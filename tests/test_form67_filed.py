"""form67_filed is set both ways, and left alone when the portal status is not a bool."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("nucleus")

from app.portal.form67_filed import maybe_apply_form67_filed


class _Db:
    def __init__(self, itr):
        self.itr = itr
        self.added = []

    async def get(self, _model, _itr_id):
        return self.itr

    def add(self, row):
        self.added.append(row)


def _job(**result):
    itr_id = uuid4()
    return SimpleNamespace(
        workflow="CHECK_FORM67",
        status="completed",
        result=result,
        itr_return_id=itr_id,
        client_id=uuid4(),
        id=uuid4(),
    )


def test_portal_true_checks_the_box() -> None:
    itr = SimpleNamespace(id=uuid4(), form67_filed=False)
    job = _job(form67_filed=True, form67_status="Successfully e-Verified")
    job.itr_return_id = itr.id
    db = _Db(itr)
    assert asyncio.run(maybe_apply_form67_filed(db, job)) is True
    assert itr.form67_filed is True
    assert db.added[0].summary == "Portal check set Form 67 filed"


def test_portal_false_clears_a_manual_tick() -> None:
    itr = SimpleNamespace(id=uuid4(), form67_filed=True)
    job = _job(form67_filed=False)
    job.itr_return_id = itr.id
    db = _Db(itr)
    assert asyncio.run(maybe_apply_form67_filed(db, job)) is True
    assert itr.form67_filed is False
    assert db.added[0].summary == "Portal check cleared Form 67 filed"


def test_already_matching_does_not_log() -> None:
    itr = SimpleNamespace(id=uuid4(), form67_filed=True)
    job = _job(form67_filed=True)
    job.itr_return_id = itr.id
    db = _Db(itr)
    assert asyncio.run(maybe_apply_form67_filed(db, job)) is False
    assert db.added == []


def test_other_status_and_failed_job_do_not_write() -> None:
    itr = SimpleNamespace(id=uuid4(), form67_filed=True)
    pending = _job(form67_filed=None, form67_status="Pending for e-verification")
    pending.itr_return_id = itr.id
    failed = _job(form67_filed=False)
    failed.status = "failed"
    failed.itr_return_id = itr.id
    db = _Db(itr)
    assert asyncio.run(maybe_apply_form67_filed(db, pending)) is False
    assert asyncio.run(maybe_apply_form67_filed(db, failed)) is False
    assert itr.form67_filed is True
    assert db.added == []
