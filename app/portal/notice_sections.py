"""Income Tax Act notice / order / penalty section catalog (advisor-facing).

Prefixes are matched longest-first against a normalized ``notice_section``
(e.g. ``271AAC(1)``, ``143(1)(a)``, ``148A``).
"""

from __future__ import annotations

from typing import Any, Optional

# (prefix, category, short_name) — longer / more specific prefixes first.
SECTION_CATALOG: list[tuple[str, str, str]] = [
    # Filing & Initial Processing
    ("139(9)", "Filing & Initial Processing", "Defective return notice"),
    ("142(1)", "Filing & Initial Processing", "Inquiry before assessment"),
    ("143(1)(A)", "Filing & Initial Processing", "Prima facie adjustment"),
    ("143(1)A", "Filing & Initial Processing", "Prima facie adjustment"),
    ("143(1)", "Filing & Initial Processing", "Intimation notice"),
    # Audit & Scrutiny
    ("143(2)", "Audit & Scrutiny", "Scrutiny assessment notice"),
    ("143(3)", "Audit & Scrutiny", "Scrutiny assessment order"),
    ("144", "Audit & Scrutiny", "Best judgment assessment"),
    # Reassessment & Reopening (1961 + 2025 Act aliases)
    ("148A", "Reassessment & Reopening", "Preliminary reopening inquiry"),
    ("148", "Reassessment & Reopening", "Income escaping assessment"),
    ("281", "Reassessment & Reopening", "Preliminary reopening inquiry"),
    ("280", "Reassessment & Reopening", "Income escaping assessment"),
    ("153A", "Reassessment & Reopening", "Search & seizure assessment"),
    ("153C", "Reassessment & Reopening", "Search & seizure assessment"),
    # Collections & Adjustments
    ("226(3)", "Collections & Adjustments", "Garnishee / bank attachment"),
    ("245", "Collections & Adjustments", "Refund adjustment notice"),
    ("156", "Collections & Adjustments", "Notice of demand"),
    # Summons & Inquiries
    ("133(6)", "Summons & Inquiries", "Power to call for information"),
    ("133A", "Summons & Inquiries", "Survey notice"),
    ("133", "Summons & Inquiries", "Power to call for information"),
    ("131", "Summons & Inquiries", "Summons for evidence"),
    # Rectifications & Revisions
    ("154", "Rectifications & Revisions", "Rectification of mistake"),
    ("250", "Rectifications & Revisions", "Appellate order intimation"),
    ("263", "Rectifications & Revisions", "Revisionary notice (Commissioner)"),
    ("264", "Rectifications & Revisions", "Revisionary application notice"),
    # Penalties & Fees (specific before generic 271*)
    ("271AAC", "Penalties & Fees", "Penalty on unexplained income"),
    ("271FAA", "Penalties & Fees", "Flawed financial reporting penalty"),
    ("271(1)(C)", "Penalties & Fees", "Concealment / inaccurate particulars penalty"),
    ("271A", "Penalties & Fees", "Failure to maintain books penalty"),
    ("271B", "Penalties & Fees", "Failure to get accounts audited penalty"),
    ("271F", "Penalties & Fees", "Late filing fees & penalties"),
    ("271H", "Penalties & Fees", "Late filing fees & penalties"),
    ("271J", "Penalties & Fees", "Penalty on professionals"),
    ("270A", "Penalties & Fees", "Under/misreporting penalty"),
    ("274", "Penalties & Fees", "Opportunity of being heard (penalty)"),
    # Common companion / interest / TDS / TP (often on portal cards)
    ("115BBE", "Penalties & Fees", "Tax on unexplained income"),
    ("234A", "Collections & Adjustments", "Interest for default in filing return"),
    ("234B", "Collections & Adjustments", "Interest for default in advance tax"),
    ("234C", "Collections & Adjustments", "Interest for deferment of advance tax"),
    ("201(1A)", "Collections & Adjustments", "Interest for late TDS deposit"),
    ("201(1)", "Collections & Adjustments", "TDS default notice"),
    ("92CA", "Audit & Scrutiny", "Transfer pricing reference"),
    ("147", "Reassessment & Reopening", "Income escaping assessment (legacy)"),
    ("149", "Reassessment & Reopening", "Time limit for notice u/s 148"),
    # Broader 143 / 139 / 142 fallbacks (after specific clauses above)
    ("143", "Audit & Scrutiny", "Assessment notice"),
    ("142", "Filing & Initial Processing", "Inquiry before assessment"),
    ("139", "Filing & Initial Processing", "Return-related notice"),
]


def normalize_section(section: str | None) -> str:
    if not section:
        return ""
    return str(section).strip().upper().replace(" ", "")


def classify_notice_section(section: str | None) -> dict[str, Any]:
    """Return ``{section, category, short_name}`` for a scraped notice section."""
    raw = (section or "").strip()
    key = normalize_section(raw)
    if not key:
        return {
            "section": None,
            "category": None,
            "short_name": None,
        }
    for prefix, category, short_name in SECTION_CATALOG:
        if key.startswith(normalize_section(prefix)):
            return {
                "section": raw,
                "category": category,
                "short_name": short_name,
            }
    return {
        "section": raw,
        "category": None,
        "short_name": None,
    }


def section_short_name(section: str | None, *, default: str = "E-proceeding notice") -> str:
    classified = classify_notice_section(section)
    return classified["short_name"] or default
