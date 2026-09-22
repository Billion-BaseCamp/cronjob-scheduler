from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.portal.workflows.check_itr_status_ay import run_check_itr_status_ay
from app.portal.workflows.check_itr_verification import run_check_itr_verification
from app.portal.workflows.check_pending_actions_notices import (
    run_check_pending_actions_notices,
)

Runner = Callable[[Any, Any], Awaitable[None]]

REGISTRY: dict[str, Runner] = {
    "CHECK_ITR_VERIFICATION": run_check_itr_verification,
    "CHECK_ITR_STATUS_AY": run_check_itr_status_ay,
    "CHECK_PENDING_ACTIONS_NOTICES": run_check_pending_actions_notices,
}


def get_runner(name: str) -> Runner | None:
    return REGISTRY.get((name or "").strip().upper())
