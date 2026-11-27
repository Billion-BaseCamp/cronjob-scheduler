"""Form 168 / ATS (legacy 26AS) TXT helpers: FY labels, S3 keys, light validation."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from uuid import UUID

from app.portal.ais_json import normalize_financial_year
from app.portal.foreign_assets import normalize_pan
from app.service.quarter_transition import get_current_quarter_from_date

# Portal Tax Year dropdown label (Form 168 ATS from TY 2026–27).
DEFAULT_PORTAL_TAX_YEAR = "2026-27"

S3_26AS_ROOT_PREFIX = "advance-tax-26as"

_PAN_IN_TEXT_RE = re.compile(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b")
_FY_IN_TEXT_RE = re.compile(
    r"(?:Financial\s*Year|F\.?\s*Y\.?|Tax\s*Year)\s*[:\-]?\s*"
    r"(20\d{2}\s*[-/]\s*(?:20)?\d{2}|\d{2}\s*[-/]\s*\d{2})",
    re.I,
)


def portal_tax_year(raw: str | None = None) -> str:
    """Normalize job year to portal Tax Year ``YYYY-YY`` (default ``2026-27``)."""
    if raw is None or not str(raw).strip():
        return DEFAULT_PORTAL_TAX_YEAR
    return normalize_financial_year(raw)


def db_financial_year(raw: str | None = None) -> str:
    """Advance-tax ``financial_years.financial_year`` label ``YY-YY`` (e.g. ``26-27``)."""
    full = portal_tax_year(raw)
    start = int(full.split("-")[0])
    end_two = int(full.split("-")[1])
    return f"{start % 100:02d}-{end_two:02d}"


def current_quarter_number(today: date | None = None) -> int:
    """India FY quarter number (Q1 Apr–Jun … Q4 Jan–Mar)."""
    _, quarter_number = get_current_quarter_from_date(today or date.today())
    return quarter_number


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def form_168_txt_filename(pan: str, tax_year: str) -> str:
    clean_pan = normalize_pan(pan) or "UNKNOWN"
    fy = db_financial_year(tax_year)
    return f"{clean_pan}-{fy}.txt"


def form_168_s3_key(
    client_id: UUID | str,
    tax_year: str,
    quarter_number: int,
    document_id: UUID | str,
) -> str:
    """Match advance-tax ``build_26as_s3_key`` layout."""
    cid = str(client_id or "").strip()
    if not cid or "/" in cid or "\\" in cid or ".." in cid:
        raise ValueError("client_id cannot be used in an S3 key")
    if quarter_number < 1 or quarter_number > 4:
        raise ValueError(f"invalid quarter_number: {quarter_number}")
    doc = str(document_id or "").strip()
    if not doc or "/" in doc or "\\" in doc or ".." in doc:
        raise ValueError("document_id cannot be used in an S3 key")
    fy = db_financial_year(tax_year)
    return f"{S3_26AS_ROOT_PREFIX}/{fy}/{cid}/{quarter_number}/{doc}"


def extract_pan_from_txt(raw: bytes) -> str | None:
    text = raw.decode("utf-8", errors="replace")
    match = _PAN_IN_TEXT_RE.search(text.upper())
    return match.group(1) if match else None


def extract_fy_from_txt(raw: bytes) -> str | None:
    text = raw.decode("utf-8", errors="replace")
    match = _FY_IN_TEXT_RE.search(text)
    if not match:
        return None
    try:
        return normalize_financial_year(match.group(1))
    except ValueError:
        return None


def is_valid_form_168_txt(raw: bytes | None) -> bool:
    if not raw or len(raw) < 32:
        return False
    # Reject obvious HTML error pages.
    head = raw[:200].lstrip().lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html"):
        return False
    return True
