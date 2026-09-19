from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.portal.workflows.check_itr_verification import run_check_itr_verification

Runner = Callable[[Any, Any], Awaitable[None]]

REGISTRY: dict[str, Runner] = {
    "CHECK_ITR_VERIFICATION": run_check_itr_verification,
}


def get_runner(name: str) -> Runner | None:
    return REGISTRY.get((name or "").strip().upper())
