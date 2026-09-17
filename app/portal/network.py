"""Portal login HTTP helpers.

The form posts to /iec/loginapi/login. HTTP status is often 200 even
when login failed — the real error is in JSON `messages`.
Passwords in that JSON are redacted before logging.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from playwright.async_api import Page, Response

logger = logging.getLogger(__name__)

_SECRET_KEYS = ("pass", "password", "otp", "authToken", "token")


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
        return " ".join(part for part in (code, desc) if part) or "ERROR"
    return None


def _redact(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key in _SECRET_KEYS:
            out[key] = "***"
        elif isinstance(item, dict):
            out[key] = _redact(item)
        else:
            out[key] = item
    return out


def _body_for_log(text: str) -> str:
    try:
        text = json.dumps(
            _redact(json.loads(text)), separators=(",", ":")
        )
    except Exception:
        pass
    return text.replace("\n", " ")[:400]


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
        response.url,
        _body_for_log(body),
    )
