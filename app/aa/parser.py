"""Parse Finsense FI payloads into normalised rows.

Two rules, both of which protect real money.

**Decimal(str(value)), never Decimal(float).** ``Decimal(0.1)`` yields
0.1000000000000000055511151231257827, because the float was already wrong
before Decimal saw it. Going via ``str`` preserves what the bank actually sent.

**Dedupe hashes narration too.** Both vendor sample payloads show
``"txnId": ""`` — banks frequently omit it — so a key of timestamp+amount alone
merges two genuine 100 rupee UPI payments made on the same day into one row.
Including narration risks keeping a rare true duplicate; excluding it risks
destroying a real transaction. Losing real financial data is the worse failure.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

logger = logging.getLogger(__name__)


def to_decimal(value: Any) -> Optional[Decimal]:
    """Parse money without ever passing through float."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        logger.warning("Unparseable amount in FI payload: %r", value)
        return None


def to_datetime(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, always returning a tz-aware value.

    A naive timestamp stored in a timestamptz column is silently interpreted as
    the server's zone; for an Indian bank feed that shifts every transaction by
    5h30m and quietly moves transactions across financial-year boundaries.
    """
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            logger.warning("Unparseable timestamp in FI payload: %r", value)
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_date(value: Any) -> Optional[date]:
    dt = to_datetime(value)
    return dt.date() if dt else None


def _first(obj: dict[str, Any], *names: str) -> Any:
    """Vendor payloads vary in casing between FIPs; try each spelling."""
    for n in names:
        if n in obj and obj[n] not in (None, ""):
            return obj[n]
    return None


def make_dedupe_key(
    *,
    txn_id: Optional[str],
    timestamp: Optional[datetime],
    amount: Optional[Decimal],
    narration: Optional[str],
) -> str:
    parts = [
        (txn_id or "").strip(),
        timestamp.isoformat() if timestamp else "",
        str(amount) if amount is not None else "",
        (narration or "").strip().lower(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def parse_transaction(raw: dict[str, Any]) -> dict[str, Any]:
    """One vendor transaction object -> kwargs for AATransaction."""
    amount = to_decimal(_first(raw, "amount", "Amount"))
    timestamp = to_datetime(
        _first(raw, "transactionTimestamp", "valueDate", "transactionDate")
    )
    narration = _first(raw, "narration", "Narration", "remarks", "description")
    txn_id = _first(raw, "txnId", "transactionId", "txnid")

    txn_type = _first(raw, "type", "Type", "transactionType")
    if isinstance(txn_type, str):
        txn_type = txn_type.strip().upper() or None

    return {
        "txn_type": txn_type,
        "amount": amount,
        "currency": _first(raw, "currency", "Currency") or "INR",
        "balance_after": to_decimal(_first(raw, "currentBalance", "balance")),
        "txn_timestamp": timestamp,
        "value_date": to_date(_first(raw, "valueDate", "transactionDate")),
        "mode": _first(raw, "mode", "Mode"),
        "reference": _first(raw, "reference", "referenceNumber", "chequeNumber"),
        "txn_id": txn_id,
        "narration": narration,
        "raw": raw,
        "dedupe_key": make_dedupe_key(
            txn_id=txn_id, timestamp=timestamp, amount=amount, narration=narration
        ),
    }


def parse_account_block(block: dict[str, Any]) -> dict[str, Any]:
    """One FIP account block -> summary fields plus its transactions.

    The vendor nests this differently across FI types, so the lookups are
    deliberately forgiving: a missing Summary must not lose the Transactions.
    """
    account = block.get("account") or block.get("Account") or block
    summary = account.get("Summary") or account.get("summary") or {}
    profile = account.get("Profile") or account.get("profile") or {}

    txn_container = account.get("Transactions") or account.get("transactions") or {}
    if isinstance(txn_container, list):
        txn_list = txn_container
    else:
        txn_list = (
            txn_container.get("Transaction")
            or txn_container.get("transaction")
            or []
        )
    if isinstance(txn_list, dict):  # single transaction comes back unwrapped
        txn_list = [txn_list]

    balance = to_decimal(
        _first(summary, "currentBalance", "CurrentBalance", "balance")
    )

    return {
        "fi_type": _first(block, "fiType", "FIType", "type"),
        "masked_account_number": _first(
            block, "maskedAccNumber", "maskedAccountNumber", "linkedAccRef"
        ),
        "link_ref_number": _first(block, "linkRefNumber", "linkReferenceNumber"),
        "balance_amount": balance,
        "balance_as_of": to_datetime(
            _first(summary, "balanceDateTime", "asOnDate", "currentBalanceDateTime")
        ),
        "currency": _first(summary, "currency", "Currency") or "INR",
        "summary": summary or None,
        "holder_profile": profile or None,
        "transactions": [parse_transaction(t) for t in txn_list if isinstance(t, dict)],
    }


def parse_fi_payload(payload: Any) -> list[dict[str, Any]]:
    """Top-level /FIDataFetch response -> a list of account blocks."""
    blocks: list[dict[str, Any]] = []

    if isinstance(payload, dict):
        candidates = (
            payload.get("body")
            or payload.get("fiObjects")
            or payload.get("FI")
            or payload.get("data")
            or []
        )
    else:
        candidates = payload or []

    if isinstance(candidates, dict):
        candidates = [candidates]

    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("fiObjects") or entry.get("data") or entry.get("Account")
        if isinstance(inner, list):
            for sub in inner:
                if isinstance(sub, dict):
                    parsed = parse_account_block(sub)
                    parsed.setdefault("fip_id", _first(entry, "fipId", "FIP", "fipID"))
                    blocks.append(parsed)
        else:
            parsed = parse_account_block(entry)
            parsed.setdefault("fip_id", _first(entry, "fipId", "FIP", "fipID"))
            blocks.append(parsed)

    return blocks
