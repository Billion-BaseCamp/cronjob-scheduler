"""AIS JSON S3 key, financial year normalization, captcha sanitization, and payload validation."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from app.portal.foreign_assets import normalize_pan

_FY_FULL_RE = re.compile(
    r"^(?:F\.?Y\.?\s*)?(20\d{2})\s*[-/]\s*(?:20)?(\d{2})$", re.I
)
_FY_SHORT_RE = re.compile(r"^(?:F\.?Y\.?\s*)?(\d{2})\s*[-/]\s*(\d{2})$", re.I)
_HEX_HEAD_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CAPTCHA_TOKEN_RE = re.compile(r"[A-Za-z0-9]{4,8}")


def normalize_financial_year(raw: str | None) -> str:
    """Normalize an Indian financial year label to ``YYYY-YY`` (e.g. ``2025-26``).

    Raises ``ValueError`` for blank, malformed, or path-unsafe values.
    """
    text = (raw or "").strip()
    if not text or "\\" in text or ".." in text:
        raise ValueError("financial year is invalid")

    match = _FY_FULL_RE.match(text)
    if match:
        start = int(match.group(1))
        end_two = int(match.group(2))
        if end_two != (start + 1) % 100:
            raise ValueError(f"invalid financial year span: {text!r}")
        return f"{start}-{end_two:02d}"

    short = _FY_SHORT_RE.match(text)
    if short:
        start = 2000 + int(short.group(1))
        end_two = int(short.group(2))
        if end_two != (start + 1) % 100:
            raise ValueError(f"invalid financial year span: {text!r}")
        return f"{start}-{end_two:02d}"

    raise ValueError(f"unsupported financial year format: {text!r}")


def safe_financial_year(raw: str | None) -> str | None:
    try:
        return normalize_financial_year(raw)
    except ValueError:
        return None


def ais_json_filename(pan: str, financial_year: str) -> str:
    clean_pan = normalize_pan(pan)
    if not clean_pan:
        raise ValueError("valid PAN is required for AIS JSON filename")
    fy = normalize_financial_year(financial_year)
    return f"{clean_pan}_{fy}.json"


def ais_json_s3_key(
    client_id: UUID | str,
    pan: str,
    financial_year: str,
) -> str:
    """Deterministic S3 key: ``portal-automation/ais-json/{client_id}/{FY_YEAR}/{PAN}_{FY_YEAR}.json``."""
    cid = str(client_id or "").strip()
    if not cid or "/" in cid or "\\" in cid or ".." in cid:
        raise ValueError("client_id cannot be used in an S3 key")
    fy = normalize_financial_year(financial_year)
    filename = ais_json_filename(pan, fy)
    return f"portal-automation/ais-json/{cid}/{fy}/{filename}"


def sanitize_captcha_text(raw: str | None) -> str | None:
    """Extract a clean alphanumeric captcha solution from LLM output."""
    if not raw:
        return None
    collapsed = re.sub(r"[^A-Za-z0-9]", "", raw.strip())
    if not collapsed:
        return None
    if 4 <= len(collapsed) <= 8:
        return collapsed
    matches = _CAPTCHA_TOKEN_RE.findall(raw)
    return matches[-1] if matches else None


def looks_like_encrypted_ais_bytes(raw: bytes) -> bool:
    """Portal AIS Utility downloads start with 64 hex chars (IV + salt) followed by ciphertext."""
    if len(raw) < 80:
        return False
    try:
        head = raw[:64].decode("ascii")
    except UnicodeDecodeError:
        return False
    return bool(_HEX_HEAD_RE.match(head))


def is_valid_ais_download(raw: bytes) -> bool:
    """True when downloaded bytes are either an encrypted AIS export or plain AIS JSON."""
    if not raw or not raw.strip():
        return False
    if looks_like_encrypted_ais_bytes(raw):
        return True
    try:
        doc: Any = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(doc, dict):
        return False
    return any(key in doc for key in ("partB", "partA", "header", "ais"))
