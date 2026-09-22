"""Which portal page is stored as the one primary screenshot."""

from __future__ import annotations

EVIDENCE_LOGIN = "LOGIN"
EVIDENCE_FILED_RETURNS = "VIEW_FILED_RETURNS"
EVIDENCE_LIFECYCLE = "LIFECYCLE"
EVIDENCE_E_PROCEEDINGS = "E_PROCEEDINGS"


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


def primary_notices_evidence_step(
    *,
    login_ok: bool,
    harvested_ok: bool,
) -> str:
    if not login_ok:
        return EVIDENCE_LOGIN
    if harvested_ok:
        return EVIDENCE_E_PROCEEDINGS
    return EVIDENCE_E_PROCEEDINGS
