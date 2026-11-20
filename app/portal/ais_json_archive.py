"""Record a downloaded AIS JSON in ``itr_ais_json_archives`` for Tax Engine import."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dialect_name(db: AsyncSession) -> str | None:
    bind = getattr(db, "bind", None)
    if bind is None and hasattr(db, "get_bind"):
        try:
            bind = db.get_bind()
        except Exception:
            bind = None
    name = getattr(getattr(bind, "dialect", None), "name", None)
    return name if isinstance(name, str) else None


async def remember_ais_json_archive(
    db: AsyncSession,
    *,
    client_id: UUID,
    itr_return_id: UUID | None,
    s3_key: str,
    original_filename: str,
    file_size_bytes: int,
) -> None:
    """Upsert an ``ITRAisJsonArchive`` row for this deterministic S3 key.

    Because portal-automation overwrites ``portal-automation/ais-json/{client_id}/{FY}/{PAN}_{FY}.json``,
    reusing an existing row with the same ``(client_id, s3_key)`` avoids duplicate
    archive records and prevents FIFO cleanup from deleting the active S3 object.
    """
    try:
        from nucleus.models.itr_filling.ais_json_archive import ITRAisJsonArchive
    except ImportError:
        logger.warning(
            "ITRAisJsonArchive is unavailable on this nucleus release; skipping archive row"
        )
        return

    # Serialize concurrent upserts for the same (client_id, s3_key) without
    # requiring a schema-level unique constraint on itr_ais_json_archives.
    if _dialect_name(db) != "sqlite":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"ais-archive:{client_id}:{s3_key}"},
        )

    existing = (
        await db.execute(
            select(ITRAisJsonArchive)
            .where(
                ITRAisJsonArchive.client_id == client_id,
                ITRAisJsonArchive.s3_key == s3_key,
            )
            .order_by(ITRAisJsonArchive.created_at.desc())
            .limit(1)
        )
    ).scalars().first()

    if existing is not None:
        if itr_return_id is not None and existing.itr_return_id is None:
            existing.itr_return_id = itr_return_id
        existing.original_filename = original_filename[:255]
        existing.file_size_bytes = file_size_bytes
        existing.updated_at = _now()
        await db.flush()
        return

    db.add(
        ITRAisJsonArchive(
            id=uuid4(),
            client_id=client_id,
            itr_return_id=itr_return_id,
            s3_key=s3_key,
            original_filename=original_filename[:255],
            file_size_bytes=file_size_bytes,
        )
    )
    await db.flush()
