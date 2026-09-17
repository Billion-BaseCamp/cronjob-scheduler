"""Single-instance guard for background loops.

A bare ``asyncio.create_task`` in ``lifespan`` starts one loop **per process**.
That is fine for a claim loop protected by ``FOR UPDATE SKIP LOCKED``, but it is
wrong for a sweeper: every replica would re-queue the same stale jobs
simultaneously, multiplying work and racing each other's writes.

A Postgres advisory lock is the cheapest correct fix. It needs no new
infrastructure, it is held on a dedicated connection for the lifetime of the
process, and it is released automatically if the process dies — so a crashed
instance does not block its replacement.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text

from app.db.database import async_engine

logger = logging.getLogger(__name__)

# Arbitrary but stable. Distinct keys let the portal and AA sweepers run on
# different instances rather than contending for one lock.
AA_SWEEPER_LOCK_KEY = 4823_0001
PORTAL_SWEEPER_LOCK_KEY = 4823_0002


@asynccontextmanager
async def advisory_lock(key: int, *, name: str) -> AsyncIterator[bool]:
    """Hold a session-level advisory lock for the duration of the block.

    Yields ``True`` when this process owns the lock, ``False`` when another
    instance already holds it. The connection is kept open deliberately: a
    session-level lock lives with its connection, so returning it to the pool
    would release the lock immediately.
    """
    conn = await async_engine.connect()
    acquired = False
    try:
        acquired = bool(
            await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
        )
        if acquired:
            logger.info("%s: advisory lock %s acquired", name, key)
        else:
            logger.info(
                "%s: another instance holds advisory lock %s — not starting", name, key
            )
        yield acquired
    finally:
        # Release explicitly before returning the connection. SQLAlchemy's
        # async connections sit on a pooled DBAPI connection, so closing does
        # NOT necessarily end the Postgres session — the lock would survive and
        # never be re-acquirable by this process. Verified: without this,
        # a second acquisition after the first holder exits fails.
        if acquired:
            try:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                await conn.commit()
                logger.info("%s: advisory lock %s released", name, key)
            except Exception:  # noqa: BLE001
                logger.warning("%s: failed to release advisory lock %s", name, key)
        try:
            await conn.close()
        except Exception:  # noqa: BLE001
            logger.warning("%s: failed to close advisory-lock connection", name)
