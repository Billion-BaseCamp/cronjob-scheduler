"""Set itr_returns.form67_filed from a completed CHECK_FORM67 job.

True and false both overwrite a manual tick. A missing flag (other portal
status, dry-run, or a failed job) does not change the checkbox.
"""

from __future__ import annotations

import logging

from nucleus.models import ITRActivityLog, ITRReturn

logger = logging.getLogger(__name__)

_WORKFLOW = "CHECK_FORM67"


async def maybe_apply_form67_filed(db, job) -> bool:
    if getattr(job, "workflow", None) != _WORKFLOW:
        return False
    if getattr(job, "status", None) != "completed":
        return False
    result = job.result if isinstance(job.result, dict) else {}
    filed = result.get("form67_filed")
    if not isinstance(filed, bool):
        return False
    itr_id = getattr(job, "itr_return_id", None)
    if itr_id is None:
        return False
    itr = await db.get(ITRReturn, itr_id)
    if itr is None:
        return False
    if bool(itr.form67_filed) == filed:
        return False
    itr.form67_filed = filed
    db.add(
        ITRActivityLog(
            action="update",
            entity_table="itr_returns",
            entity_id=itr.id,
            itr_return_id=itr.id,
            client_id=job.client_id,
            summary=(
                "Portal check set Form 67 filed"
                if filed
                else "Portal check cleared Form 67 filed"
            ),
            payload={
                "form67_filed": filed,
                "workflow": getattr(job, "workflow", None),
                "job_id": str(job.id),
                "form67_status": result.get("form67_status"),
                "acknowledgement_no": result.get("acknowledgement_no"),
                "filing_date": result.get("filing_date"),
            },
        )
    )
    logger.info(
        "Set itr %s form67_filed=%s from portal job %s",
        itr.id,
        filed,
        job.id,
    )
    return True
