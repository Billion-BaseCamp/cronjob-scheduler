"""Solve AIS portal captcha images via the Gemini generateContent API.

Supports multi-key load balancing via ``GEMINI_API_KEY_1``…``GEMINI_API_KEY_4``,
with round-robin selection and skip-on-429 (rate-limited keys are retired for
the life of the process).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
from typing import Any

import requests

from app.core.config import settings
from app.portal.ais_json import sanitize_captcha_text

logger = logging.getLogger(__name__)

_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

_CAPTCHA_PROMPT = (
    "Read the exact alphanumeric CAPTCHA text shown in this image. "
    "Ignore the thin green diagonal/criss-cross background lines. "
    "Output ONLY the exact characters with no spaces, punctuation, quotes, or explanation."
)

_rr_lock = threading.Lock()
_rr_index = 0
_rate_limited_keys: set[str] = set()


class CaptchaSolveError(Exception):
    """Gemini could not produce a usable captcha answer."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class GeminiRateLimitError(CaptchaSolveError):
    """Gemini rejected the request due to rate / quota limits (HTTP 429)."""


def gemini_api_keys() -> list[str]:
    """Configured Gemini keys from ``GEMINI_API_KEY_1``…``_4`` (plus legacy ``GEMINI_API_KEY``)."""
    keys: list[str] = []
    for attr in (
        "GEMINI_API_KEY_1",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_3",
        "GEMINI_API_KEY_4",
    ):
        key = (getattr(settings, attr, "") or "").strip()
        if key and key not in keys:
            keys.append(key)
    # Slot 1 fallback for existing deploys that only set GEMINI_API_KEY.
    if not (settings.GEMINI_API_KEY_1 or "").strip():
        legacy = (settings.GEMINI_API_KEY or "").strip()
        if legacy and legacy not in keys:
            keys.insert(0, legacy)
    return keys


def _mark_rate_limited(api_key: str) -> None:
    with _rr_lock:
        _rate_limited_keys.add(api_key)
    logger.warning(
        "Gemini API key marked rate-limited (suffix …%s); skipping for this process",
        api_key[-4:] if len(api_key) >= 4 else "????",
    )


def _available_keys() -> list[str]:
    keys = gemini_api_keys()
    with _rr_lock:
        return [k for k in keys if k not in _rate_limited_keys]


def _round_robin_key_order() -> list[str]:
    """Return available keys rotated so the next request starts at the RR cursor."""
    global _rr_index
    available = _available_keys()
    if not available:
        return []
    with _rr_lock:
        start = _rr_index % len(available)
        _rr_index += 1
        return available[start:] + available[:start]


def reset_gemini_key_pool_for_tests() -> None:
    """Reset round-robin / rate-limit state (tests only)."""
    global _rr_index
    with _rr_lock:
        _rr_index = 0
        _rate_limited_keys.clear()


def _extract_candidate_text(body: dict[str, Any]) -> str:
    candidates = body.get("candidates") or []
    for candidate in candidates:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _error_detail(response: requests.Response) -> str:
    try:
        err_body = response.json()
        msg = ((err_body or {}).get("error") or {}).get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()[:300]
    except ValueError:
        pass
    return ""


def _is_rate_limited(status_code: int, detail: str) -> bool:
    if status_code == 429:
        return True
    upper = detail.upper()
    return "RESOURCE_EXHAUSTED" in upper or "RATE LIMIT" in upper or "QUOTA" in upper


def _post_gemini(
    png_bytes: bytes,
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    url = _GEMINI_ENDPOINT.format(model=model)
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": _CAPTCHA_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": "image/png",
                            "data": base64.b64encode(png_bytes).decode("ascii"),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 256,
        },
    }
    response = requests.post(
        url,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        json=payload,
        timeout=timeout_seconds,
    )
    if response.status_code >= 400:
        detail = _error_detail(response)
        suffix = f": {detail}" if detail else ""
        if _is_rate_limited(response.status_code, detail):
            raise GeminiRateLimitError(
                f"Gemini API rate-limited HTTP {response.status_code}{suffix}"
            )
        raise CaptchaSolveError(
            f"Gemini API returned HTTP {response.status_code}{suffix}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise CaptchaSolveError("Gemini API returned invalid JSON") from exc


async def _solve_with_key(
    png_bytes: bytes,
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> str:
    try:
        body = await asyncio.to_thread(
            _post_gemini,
            png_bytes,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    except GeminiRateLimitError:
        raise
    except CaptchaSolveError:
        raise
    except requests.RequestException as exc:
        logger.warning("Gemini captcha request failed: %s", exc)
        raise CaptchaSolveError(f"Gemini request failed: {exc}") from exc

    raw_text = _extract_candidate_text(body)
    cleaned = sanitize_captcha_text(raw_text)
    if not cleaned:
        raise CaptchaSolveError(
            f"Gemini returned an unusable captcha answer: {raw_text!r}"
        )
    return cleaned


async def solve_captcha_with_gemini(
    png_bytes: bytes,
    *,
    api_key: str | None = None,
    model: str | None = None,
    timeout_seconds: float | None = None,
) -> str:
    """Send captcha PNG bytes to Gemini and return sanitized alphanumeric characters.

    When ``api_key`` is omitted, keys from ``GEMINI_API_KEY_1``…``_4``
    (or legacy ``GEMINI_API_KEY``) are tried in round-robin order. Keys that
    return HTTP 429 are skipped for the rest of the process.
    """
    if not png_bytes:
        raise CaptchaSolveError("Captcha image was empty.")

    resolved_model = (
        model if model is not None else settings.GEMINI_CAPTCHA_MODEL
    ).strip() or "gemini-3.1-flash-lite"
    resolved_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else settings.GEMINI_CAPTCHA_TIMEOUT_SECONDS
    )

    if api_key is not None:
        key = api_key.strip()
        if not key:
            raise CaptchaSolveError("GEMINI_API_KEY is not configured.")
        return await _solve_with_key(
            png_bytes,
            api_key=key,
            model=resolved_model,
            timeout_seconds=resolved_timeout,
        )

    key_order = _round_robin_key_order()
    if not key_order:
        configured = gemini_api_keys()
        if not configured:
            raise CaptchaSolveError(
                "No Gemini API keys configured "
                "(set GEMINI_API_KEY_1…GEMINI_API_KEY_4 or GEMINI_API_KEY)."
            )
        raise CaptchaSolveError(
            "All configured Gemini API keys are rate-limited for this process."
        )

    errors: list[str] = []
    for key in key_order:
        try:
            answer = await _solve_with_key(
                png_bytes,
                api_key=key,
                model=resolved_model,
                timeout_seconds=resolved_timeout,
            )
            if key != key_order[0]:
                logger.info(
                    "Captcha solved with alternate Gemini key (suffix …%s)",
                    key[-4:] if len(key) >= 4 else "????",
                )
            return answer
        except GeminiRateLimitError as exc:
            _mark_rate_limited(key)
            errors.append(exc.message)
            continue

    joined = "; ".join(errors) if errors else "unknown"
    raise CaptchaSolveError(
        f"All available Gemini API keys were rate-limited ({joined})"
    )
