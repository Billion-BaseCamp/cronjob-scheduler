"""Which portal page is stored as the one primary screenshot."""

from __future__ import annotations

EVIDENCE_LOGIN = "LOGIN"
EVIDENCE_FILED_RETURNS = "VIEW_FILED_RETURNS"
EVIDENCE_LIFECYCLE = "LIFECYCLE"


def primary_evidence_step(
    *,
    login_ok: bool,
    lifecycle_visible: bool,
) -> str:
    if not login_ok:
        return EVIDENCE_LOGIN
    if lifecycle_visible:
        return EVIDENCE_LIFECYCLE
    return EVIDENCE_FILED_RETURNS
