"""Which filed-return card supplies the ITR file, and where that file is stored.

AY 2021-22 onward is JSON. AY 2020-21 and earlier is XML. Cards are ordered by
Filing Date, newest first. Rectification and Revised keep walking when the
download button is missing. Any other filing type stops the walk: download it
when the button is there, otherwise there is no file.
``latest_filing_type`` and ``latest_filing_date`` come from that newest card.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence
from uuid import UUID

_FILING_TYPE_RE = re.compile(r"Filing Type\s*:?\s*([A-Za-z]+)", re.I)
_ITR_FORM_RE = re.compile(r"ITR\s*:\s*(ITR-\d+[A-Za-z]*)", re.I)
_ACK_RE = re.compile(r"Acknowledgement No\.?\s*:?\s*(\d+)", re.I)
_SECTION_RE = re.compile(r"Filing Section\s*:?\s*(\d+(?:\([^)]+\))?)", re.I)
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
_E_VERIFIED_RE = re.compile(
    r"Successfully e-verified\s*(?:on\s*)?"
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s+\d{1,2},\s+\d{4})",
    re.I,
)
_DATE_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s+(\d{1,2}),\s+(\d{4})",
    re.I,
)
_WALK_TYPES = frozenset({"rectification", "revised"})

JSON_NOT_AVAILABLE = "JSON_NOT_AVAILABLE"
XML_NOT_AVAILABLE = "XML_NOT_AVAILABLE"
JSON_FROM_START_YEAR = 2021


@dataclass(frozen=True)
class FiledReturnCard:
    filing_type: str
    filing_date: datetime | None
    has_download_json: bool  # this year's Download JSON or Download XML button
    page_index: int = 0
    card_index: int = 0
    itr_form: str = ""
    acknowledgement_no: str = ""
    filing_section: str = ""
    e_verified_on: str = ""


def _iso_date(text: str) -> str:
    match = _DATE_RE.search(text or "")
    if match is None:
        return ""
    month = _MONTHS.get(match.group(1).lower())
    if month is None:
        return ""
    try:
        found = datetime(int(match.group(3)), month, int(match.group(2)))
    except ValueError:
        return ""
    return found.date().isoformat()


def parse_filing_type(text: str) -> str:
    match = _FILING_TYPE_RE.search(text or "")
    if match is None:
        return ""
    return match.group(1).strip()


def parse_itr_form(text: str) -> str:
    match = _ITR_FORM_RE.search(text or "")
    if match is None:
        return ""
    return match.group(1).upper()


def parse_acknowledgement_no(text: str) -> str:
    match = _ACK_RE.search(text or "")
    if match is None:
        return ""
    return match.group(1)


def parse_filing_section(text: str) -> str:
    match = _SECTION_RE.search(text or "")
    if match is None:
        return ""
    return match.group(1)


def parse_e_verified_on(text: str) -> str:
    """Date under ``Successfully e-verified``, or empty when that step is absent."""
    match = _E_VERIFIED_RE.search(text or "")
    if match is None:
        return ""
    return _iso_date(match.group(1))


def assessment_years_from_job(job: object) -> list[str]:
    """Years queued on the job. One year still uses ``assessment_year``."""
    result = getattr(job, "result", None)
    years: list[str] = []
    if isinstance(result, dict):
        raw = result.get("assessment_years")
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, str):
                    continue
                text = item.strip()
                if text and text not in years:
                    years.append(text)
    if years:
        return years
    single = (getattr(job, "assessment_year", None) or "").strip()
    return [single] if single else []


def itr_file_format(assessment_year: str) -> str:
    """``json`` from AY 2021-22. ``xml`` through AY 2020-21."""
    start = (assessment_year or "").strip().split("-", 1)[0]
    try:
        start_year = int(start)
    except ValueError:
        return "json"
    return "json" if start_year >= JSON_FROM_START_YEAR else "xml"


def download_button_label(file_format: str) -> str:
    return "Download XML" if file_format == "xml" else "Download JSON"


def missing_file_code(file_format: str) -> str:
    return XML_NOT_AVAILABLE if file_format == "xml" else JSON_NOT_AVAILABLE


def _safe_assessment_year(assessment_year: str) -> str:
    ay = (assessment_year or "").strip()
    if not ay or "/" in ay or "\\" in ay or ".." in ay:
        raise ValueError("assessment year cannot be used in an S3 key")
    return ay


def itr_json_s3_key(client_id: UUID | str, assessment_year: str) -> str:
    """One JSON object per client and assessment year. The next download overwrites it."""
    ay = _safe_assessment_year(assessment_year)
    return f"portal-automation/json/{client_id}/{ay}/itr.json"


def itr_xml_s3_key(client_id: UUID | str, assessment_year: str) -> str:
    """One XML object per client and assessment year. The next download overwrites it."""
    ay = _safe_assessment_year(assessment_year)
    return f"portal-automation/xml/{client_id}/{ay}/itr.xml"


def itr_file_s3_key(
    client_id: UUID | str,
    assessment_year: str,
    file_format: str | None = None,
) -> str:
    fmt = file_format or itr_file_format(assessment_year)
    if fmt == "xml":
        return itr_xml_s3_key(client_id, assessment_year)
    return itr_json_s3_key(client_id, assessment_year)


def is_itr_json(raw: bytes) -> bool:
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(document, dict)


def is_itr_xml(raw: bytes) -> bool:
    """A parsed XML document that is not a JSON object and not an HTML page."""
    if is_itr_json(raw):
        return False
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return False
    tag = root.tag.rsplit("}", 1)[-1].lower()
    return tag not in {"html", "head", "body"}


def cards_newest_first(cards: Sequence[FiledReturnCard]) -> list[FiledReturnCard]:
    return sorted(
        cards,
        key=lambda card: (
            card.filing_date is not None,
            card.filing_date or datetime.min,
        ),
        reverse=True,
    )


def choose_itr_json_card(
    cards: Sequence[FiledReturnCard],
) -> FiledReturnCard | None:
    for card in cards_newest_first(cards):
        kind = card.filing_type.strip().casefold()
        if kind in _WALK_TYPES:
            if card.has_download_json:
                return card
            continue
        if card.has_download_json:
            return card
        return None
    return None


def latest_filing(cards: Sequence[FiledReturnCard]) -> tuple[str | None, str | None]:
    """Filing Type and Filing Date from the newest list card."""
    ordered = cards_newest_first(cards)
    if not ordered:
        return None, None
    newest = ordered[0]
    return newest.filing_type.strip() or None, downloaded_filing_date(newest)


def counts_by_filing_type(cards: Sequence[FiledReturnCard]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for card in cards:
        label = card.filing_type.strip() or "Unknown"
        counts[label] = counts.get(label, 0) + 1
    return counts


def downloaded_filing_date(card: FiledReturnCard | None) -> str | None:
    if card is None or card.filing_date is None:
        return None
    return card.filing_date.date().isoformat()
