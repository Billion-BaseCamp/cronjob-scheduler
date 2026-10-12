"""Portal login HTTP helpers.

The form posts to /iec/loginapi/login. HTTP status is often 200 even
when login failed — the real error is in JSON `messages`.
Only the portal's message codes are logged, never the body: it carries the
client's PAN, mobile and email.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from playwright.async_api import Page, Response

logger = logging.getLogger(__name__)

_PAN_RE = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", re.I)
_MAX_SUMMARY = 300


def attach_network_log(page: Page) -> None:
    """Log login API calls and HTTP errors (like the Network tab)."""
    page.on("response", _on_response)


def is_login_api(response: Response) -> bool:
    return (
        "loginapi/login" in response.url
        and response.request.method == "POST"
    )


async def login_api_error(response: Response) -> str | None:
    """Return 'EF500023 Request is not authenticated' or None if OK."""
    try:
        data = await response.json()
    except Exception:
        return None
    for msg in data.get("messages") or []:
        if str(msg.get("type", "")).upper() != "ERROR":
            continue
        code = str(msg.get("code") or "").strip()
        desc = str(msg.get("desc") or "").strip()
        return mask_pans(" ".join(part for part in (code, desc) if part)) or "ERROR"
    return None


def mask_pans(text: str) -> str:
    return _PAN_RE.sub("[PAN]", text)


def _portal_messages(data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("messages", "errors"):
        for msg in data.get(key) or []:
            if not isinstance(msg, dict):
                continue
            code = str(msg.get("code") or "").strip()
            desc = str(msg.get("desc") or "").strip()
            text = " ".join(part for part in (code, desc) if part)
            if text:
                out.append(text)
    return out


def summarize_body(text: str) -> str:
    """Portal message codes from a response body, safe to log."""
    if not text:
        return "(empty body)"
    try:
        data = json.loads(text)
    except ValueError:
        return f"(non-JSON body, {len(text)} chars)"
    if not isinstance(data, dict):
        return "(JSON body)"
    messages = _portal_messages(data)
    if not messages:
        return "(no portal messages)"
    return mask_pans("; ".join(messages))[:_MAX_SUMMARY]


async def _on_response(response: Response) -> None:
    request = response.request
    if request.resource_type not in ("xhr", "fetch"):
        return
    if "incometax.gov.in" not in response.url:
        return

    is_login = is_login_api(response)
    is_http_error = response.status >= 400
    if not is_login and not is_http_error:
        return

    try:
        body = await response.text()
    except Exception:
        body = ""
    logger.info(
        "NET %s %s %s %s",
        response.status,
        request.method,
        mask_pans(response.url),
        summarize_body(body),
    )
