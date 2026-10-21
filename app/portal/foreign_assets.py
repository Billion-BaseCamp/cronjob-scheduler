"""Foreign-assets PDF password and download-dialog rules.

The password is the portal PAN in lower case plus the date from My Profile
as ddmmyyyy, with no spaces. Example: AGSPA4774H and 02-Jan-1983 becomes
agspa4774h02011983. The date is never taken from clients.date_of_birth,
and it is not written back onto the client.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from uuid import UUID

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_PORTAL_DATE = re.compile(r"^(\d{2})-([A-Za-z]{3})-(\d{4})$")
_PAN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_NO_DATA = "No data available for the selected year"
_PASSWORD_DIALOG = "The downloaded PDF will be password protected"
_CY_MIN = 1990
_CY_MAX = 2100

# Profile labels, in the order the portal password formula uses them.
PROFILE_DATE_LABELS = (
    "Date of Birth",
    "Date of Incorporation",
    "Date of Formation",
    "Date of Incorporation/Formation",
)


def parse_portal_date(raw: str | None) -> date | None:
    """Parse a My Profile date such as ``02-Jan-1983``."""
    text = " ".join((raw or "").split())
    match = _PORTAL_DATE.match(text)
    if match is None:
        return None
    day = int(match.group(1))
    month = _MONTHS.get(match.group(2).lower())
    year = int(match.group(3))
    if month is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def normalize_pan(raw: str | None) -> str | None:
    text = "".join((raw or "").split()).upper()
    if _PAN.match(text):
        return text
    return None


def foreign_assets_password(pan: str, born_on: date) -> str:
    return f"{pan.strip().lower()}{born_on.strftime('%d%m%Y')}"


def classify_download_dialog(text: str | None) -> str:
    """``downloaded``, ``no_data``, or ``unknown``.

    Both dialogs are ``app-logout-alert``. The sentence is the only branch.
    """
    normalized = " ".join((text or "").split())
    if normalized == _NO_DATA:
        return "no_data"
    if _PASSWORD_DIALOG in normalized:
        return "downloaded"
    return "unknown"


def foreign_assets_pdf_key(job_id: UUID | str, calendar_year: int) -> str:
    return f"portal-automation/{job_id}/foreign-assets/CY-{calendar_year}.pdf"


def calendar_years_from_job(job: Any) -> list[int]:
    """Calendar years stored on the queued job.

    A SQL insert puts them on ``result``::

        {"calendar_years": [2024, 2023]}

    ``params`` is read first when that attribute exists. The completed job
    replaces ``result`` with the download outcome.
    """
    params = getattr(job, "params", None)
    years = _years_in(params)
    if years:
        return years
    result = getattr(job, "result", None)
    return _years_in(result)


def _years_in(raw: Any) -> list[int]:
    if not isinstance(raw, dict):
        return []
    values = raw.get("calendar_years")
    if not isinstance(values, list) or not values:
        return []
    years: list[int] = []
    seen: set[int] = set()
    for item in values:
        if isinstance(item, bool):
            return []
        if isinstance(item, int):
            year = item
        elif isinstance(item, str) and item.isdigit():
            year = int(item)
        else:
            return []
        if year < _CY_MIN or year > _CY_MAX:
            return []
        if year in seen:
            continue
        seen.add(year)
        years.append(year)
    return years
