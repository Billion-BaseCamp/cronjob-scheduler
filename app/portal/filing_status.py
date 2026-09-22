"""Promote itr_returns.filing_status from a completed portal check.

Only ``CHECK_ITR_VERIFICATION`` may promote. Only ``filed`` → ``e_verified``.
Never demotes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from nucleus.models import ITRActivityLog, ITRReturn

logger = logging.getLogger(__name__)

FILING_STATUS_FILED = "filed"
FILING_STATUS_E_VERIFIED = "e_verified"
_PROMOTE_WORKFLOW = "CHECK_ITR_VERIFICATION"


async def maybe_promote_e_verified(db, job) -> bool:
    if getattr(job, "workflow", None) != _PROMOTE_WORKFLOW:
        return False
    if getattr(job, "status", None) != "completed":
        return False
    result = job.result if isinstance(job.result, dict) else {}
    if result.get("e_verified") is not True:
        return False
    itr_id = getattr(job, "itr_return_id", None)
    if itr_id is None:
        return False
    itr = await db.get(ITRReturn, itr_id)
    if itr is None:
        return False
    current = (itr.filing_status or "").strip()
    if current == FILING_STATUS_E_VERIFIED:
        return False
    if current != FILING_STATUS_FILED:
        return False
    itr.filing_status = FILING_STATUS_E_VERIFIED
    if itr.filed_at is None:
        itr.filed_at = datetime.now(timezone.utc)
    db.add(
        ITRActivityLog(
            action="update",
            entity_table="itr_returns",
            entity_id=itr.id,
            itr_return_id=itr.id,
            client_id=job.client_id,
            summary="Portal check set filing status to e_verified",
            payload={
                "filing_status": itr.filing_status,
                "workflow": getattr(job, "workflow", None),
                "job_id": str(job.id),
                "itr_status": result.get("itr_status"),
            },
        )
    )
    logger.info(
        "Promoted itr %s to e_verified from portal job %s",
        itr.id,
        job.id,
    )
    return True
