"""Solve AIS portal captcha images via the Gemini generateContent API."""

from __future__ import annotations

import asyncio
import base64
import logging
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


class CaptchaSolveError(Exception):
    """Gemini could not produce a usable captcha answer."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _extract_candidate_text(body: dict[str, Any]) -> str:
    candidates = body.get("candidates") or []
    for candidate in candidates:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


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
            "thinkingConfig": {"thinkingBudget": 0},
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
        detail = ""
        try:
            err_body = response.json()
            msg = ((err_body or {}).get("error") or {}).get("message")
            if isinstance(msg, str) and msg.strip():
                detail = f": {msg.strip()[:200]}"
        except ValueError:
            pass
        raise CaptchaSolveError(
            f"Gemini API returned HTTP {response.status_code}{detail}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise CaptchaSolveError("Gemini API returned invalid JSON") from exc


async def solve_captcha_with_gemini(
    png_bytes: bytes,
    *,
    api_key: str | None = None,
    model: str | None = None,
    timeout_seconds: float | None = None,
) -> str:
    """Send captcha PNG bytes to Gemini and return sanitized alphanumeric characters."""
    if not png_bytes:
        raise CaptchaSolveError("Captcha image was empty.")
    resolved_key = (api_key if api_key is not None else settings.GEMINI_API_KEY).strip()
    if not resolved_key:
        raise CaptchaSolveError("GEMINI_API_KEY is not configured.")
    resolved_model = (
        model if model is not None else settings.GEMINI_CAPTCHA_MODEL
    ).strip() or "gemini-3.1-flash-lite"
    resolved_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else settings.GEMINI_CAPTCHA_TIMEOUT_SECONDS
    )

    try:
        body = await asyncio.to_thread(
            _post_gemini,
            png_bytes,
            api_key=resolved_key,
            model=resolved_model,
            timeout_seconds=resolved_timeout,
        )
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
