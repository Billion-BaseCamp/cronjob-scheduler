"""Which portal page is stored as the one primary screenshot."""

from __future__ import annotations

EVIDENCE_LOGIN = "LOGIN"
EVIDENCE_FILED_RETURNS = "VIEW_FILED_RETURNS"
EVIDENCE_LIFECYCLE = "LIFECYCLE"
EVIDENCE_E_PROCEEDINGS = "E_PROCEEDINGS"
EVIDENCE_OUTSTANDING_DEMAND = "OUTSTANDING_DEMAND"

NOTICE_SOURCE_EVIDENCE = {
    "e_proceedings": EVIDENCE_E_PROCEEDINGS,
    "outstanding_demand": EVIDENCE_OUTSTANDING_DEMAND,
}


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


def primary_notices_evidence_step(*, login_ok: bool, source: str) -> str:
    """Screenshot of the notice page the source harvested (also on harvest failure)."""
    if not login_ok:
        return EVIDENCE_LOGIN
    return NOTICE_SOURCE_EVIDENCE.get(source, EVIDENCE_LOGIN)
