"""Job handlers: request data, fetch it, persist it.

The pipeline with Auto-FI disabled (our configuration):

    FI_REQUEST  -> POST /FIRequest, store the sessionId
    (vendor notifies per account as data becomes ready)
    FI_FETCH    -> GET /FIDataFetch, parse, persist

``FI_REQUEST`` is built unconditionally. Even with Auto-FI enabled it only
covers the first fetch of a periodic consent; every refresh lands here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nucleus.models.account_aggregator import (
    AAAccountSnapshot,
    AAConsent,
    AACustomer,
    AAFISession,
    AAJob,
    AALinkedAccount,
    AATransaction,
    CONSENT_ACTIVE,
    FI_READY,
    FI_REQUESTED,
    JOB_TYPE_FI_FETCH,
)

from app.aa.client import FinsenseClient, FinsenseError
from app.aa.crypto import encrypt_json, encrypt_text
from app.aa.parser import parse_fi_payload
from app.core.config import settings

logger = logging.getLogger(__name__)


def _finsense_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000"


async def _load_consent(db: AsyncSession, job: AAJob) -> tuple[AAConsent, AACustomer]:
    row = (
        await db.execute(
            select(AAConsent, AACustomer)
            .join(AACustomer, AAConsent.aa_customer_id == AACustomer.id)
            .where(AAConsent.id == job.aa_consent_id)
        )
    ).first()
    if row is None:
        raise FinsenseError(f"consent {job.aa_consent_id} no longer exists", retryable=False)
    return row


async def handle_fi_request(
    db: AsyncSession, client: FinsenseClient, job: AAJob
) -> dict[str, Any]:
    """Ask the AA for data and record the resulting session."""
    consent, customer = await _load_consent(db, job)

    if consent.status != CONSENT_ACTIVE:
        # Not retryable: a revoked or rejected consent will never become active
        # again, and retrying would pointlessly hammer the vendor.
        raise FinsenseError(
            f"consent is {consent.status}, not ACTIVE", retryable=False
        )
    if not consent.consent_id:
        raise FinsenseError("consent has no consentId yet", retryable=True)

    now = datetime.now(timezone.utc)
    start = consent.fi_from or (
        now - timedelta(days=30 * settings.AA_CONSENT_DEFAULT_MONTHS)
    )
    end = consent.fi_to or now
    # The vendor rejects a window outside the consent validity, so clamp both
    # ends rather than trusting our own defaults.
    if consent.consent_start and start < consent.consent_start:
        start = consent.consent_start
    if consent.consent_expiry and end > consent.consent_expiry:
        end = consent.consent_expiry
    if end > now:
        end = now

    result = await client.fi_request(
        cust_id=customer.aa_handle,
        consent_handle=consent.consent_handle,
        consent_id=consent.consent_id,
        date_from=_finsense_ts(start),
        date_to=_finsense_ts(end),
    )

    session = AAFISession(
        aa_consent_id=consent.id,
        session_id=result["session_id"],
        txn_id=result.get("txn_id"),
        status=FI_REQUESTED,
        requested_from=start,
        requested_to=end,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    logger.info(
        "FI_REQUEST ok: consent=%s session=%s", consent.consent_handle, session.session_id
    )
    return {"session_id": result["session_id"]}


async def handle_fi_fetch(
    db: AsyncSession, client: FinsenseClient, job: AAJob
) -> dict[str, Any]:
    """Fetch the data and persist it."""
    consent, _customer = await _load_consent(db, job)

    session = None
    if job.aa_fi_session_id:
        session = (
            await db.execute(
                select(AAFISession).where(AAFISession.id == job.aa_fi_session_id)
            )
        ).scalar_one_or_none()
    if session is None:
        session = (
            await db.execute(
                select(AAFISession)
                .where(AAFISession.aa_consent_id == consent.id)
                .order_by(AAFISession.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if session is None:
        raise FinsenseError("no FI session to fetch", retryable=False)

    payload = await client.fi_data_fetch(
        consent_handle=consent.consent_handle, session_id=session.session_id
    )
    blocks = parse_fi_payload(payload)
    if not blocks:
        logger.warning(
            "FI_FETCH returned no account blocks for session %s", session.session_id
        )

    accounts_written = 0
    txns_written = 0

    for block in blocks:
        link_ref = block.get("link_ref_number")
        if not link_ref:
            logger.warning("skipping account block with no linkRefNumber")
            continue

        account = (
            await db.execute(
                select(AALinkedAccount).where(
                    AALinkedAccount.aa_consent_id == consent.id,
                    AALinkedAccount.link_ref_number == link_ref,
                )
            )
        ).scalar_one_or_none()
        if account is None:
            account = AALinkedAccount(
                aa_consent_id=consent.id,
                link_ref_number=link_ref,
                status="ACTIVE",
            )
            db.add(account)
            await db.flush()

        account.fip_id = block.get("fip_id") or account.fip_id
        account.fi_type = block.get("fi_type") or account.fi_type
        account.masked_account_number = (
            block.get("masked_account_number") or account.masked_account_number
        )
        account.last_fetched_at = datetime.now(timezone.utc)

        db.add(
            AAAccountSnapshot(
                aa_linked_account_id=account.id,
                aa_fi_session_id=session.id,
                fi_type=block.get("fi_type"),
                balance_amount=block.get("balance_amount"),
                balance_as_of=block.get("balance_as_of"),
                currency=block.get("currency"),
                summary_enc=encrypt_json(block.get("summary")),
                holder_profile_enc=encrypt_json(block.get("holder_profile")),
            )
        )
        accounts_written += 1

        for txn in block.get("transactions") or []:
            if txn.get("amount") is None:
                # An unparseable amount is worse than a missing row: it would
                # silently distort every total computed from this account.
                logger.warning(
                    "skipping transaction with unparseable amount on account %s",
                    account.id,
                )
                continue
            # ON CONFLICT DO NOTHING: a replayed fetch must be a no-op, not a
            # duplicate. The unique index on (account, dedupe_key) enforces it.
            stmt = (
                pg_insert(AATransaction.__table__)
                .values(
                    aa_linked_account_id=account.id,
                    aa_fi_session_id=session.id,
                    txn_type=txn.get("txn_type"),
                    amount=txn["amount"],
                    currency=txn.get("currency"),
                    balance_after=txn.get("balance_after"),
                    txn_timestamp=txn.get("txn_timestamp"),
                    value_date=txn.get("value_date"),
                    mode=txn.get("mode"),
                    reference=txn.get("reference"),
                    txn_id=txn.get("txn_id"),
                    narration_enc=encrypt_text(txn.get("narration")),
                    raw_enc=encrypt_json(txn.get("raw")),
                    dedupe_key=txn["dedupe_key"],
                )
                .on_conflict_do_nothing(
                    index_elements=["aa_linked_account_id", "dedupe_key"]
                )
            )
            result = await db.execute(stmt)
            txns_written += result.rowcount or 0

    session.status = FI_READY
    session.fetched_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info(
        "FI_FETCH ok: session=%s accounts=%s new_transactions=%s",
        session.session_id,
        accounts_written,
        txns_written,
    )
    return {"accounts": accounts_written, "transactions": txns_written}


HANDLERS = {
    "FI_REQUEST": handle_fi_request,
    JOB_TYPE_FI_FETCH: handle_fi_fetch,
    "REFRESH": handle_fi_request,
}
