from collections import defaultdict
from datetime import date, datetime
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.core.logger import logger
from nucleus.models.common_models.advisor import Advisor
from nucleus.models.common_models.client import Client
from nucleus.models.common_models.login import Login
from app.schema.birthday import BirthdayPerson
from app.service.email_service import send_birthday_reminder_email


def _today_ist(reference_date: Optional[date] = None) -> date:
    if reference_date is not None:
        return reference_date
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _client_display_name(client: Client) -> str:
    return f"{client.first_name or ''} {client.last_name or ''}".strip() or "Unnamed Client"


def _birthday_match_conditions(reference_date: date):
    return (
        Client.date_of_birth.isnot(None),
        func.extract("month", Client.date_of_birth) == reference_date.month,
        func.extract("day", Client.date_of_birth) == reference_date.day,
    )


def _to_birthday_person(
    person: Client,
    *,
    is_family_member: bool,
    household: Client,
) -> BirthdayPerson:
    return BirthdayPerson(
        id=person.id,
        first_name=person.first_name,
        last_name=person.last_name,
        date_of_birth=_as_date(person.date_of_birth),
        is_family_member=is_family_member,
        family_relationship=person.family_relationship,
        household_client_id=household.id,
        household_client_name=_client_display_name(household),
    )


async def get_birthdays_grouped_by_advisor(
    db: AsyncSession,
    reference_date: Optional[date] = None,
) -> dict[UUID, list[BirthdayPerson]]:
    today = _today_ist(reference_date)
    grouped: dict[UUID, list[BirthdayPerson]] = defaultdict(list)

    primary_result = await db.execute(
        select(Client).where(
            Client.is_family_member.is_(False),
            Client.advisor_id.isnot(None),
            *_birthday_match_conditions(today),
        )
    )
    for client in primary_result.scalars().all():
        grouped[client.advisor_id].append(
            _to_birthday_person(client, is_family_member=False, household=client)
        )

    parent_client = aliased(Client)
    family_result = await db.execute(
        select(Client, parent_client)
        .join(parent_client, Client.parent_id == parent_client.id)
        .where(
            Client.is_family_member.is_(True),
            parent_client.advisor_id.isnot(None),
            *_birthday_match_conditions(today),
        )
    )
    for member, parent in family_result.all():
        grouped[parent.advisor_id].append(
            _to_birthday_person(member, is_family_member=True, household=parent)
        )

    for advisor_id in grouped:
        grouped[advisor_id].sort(
            key=lambda person: (
                person.household_client_name.lower(),
                person.first_name.lower(),
                person.last_name.lower(),
            )
        )

    return dict(grouped)


async def _advisor_email_and_first_name(
    db: AsyncSession, advisor_id: UUID
) -> tuple[Optional[str], Optional[str]]:
    result = await db.execute(
        select(Login.email, Advisor.first_name)
        .join(Advisor, Advisor.id == Login.advisor_id)
        .where(
            Login.advisor_id == advisor_id,
            Login.email.isnot(None),
            Login.email.like("%@%"),
        )
        .limit(1)
    )
    row = result.first()
    if not row:
        return None, None
    email = (row.email or "").strip() or None
    first_name = (row.first_name or "").strip() or None
    return email, first_name


async def process_birthday_emails_for_day(
    db: AsyncSession,
    reference_date: Optional[date] = None,
) -> None:
    """One Graph email per RM with client/family birthdays today. No in-app or push."""
    today = _today_ist(reference_date)
    grouped = await get_birthdays_grouped_by_advisor(db, today)

    email_sent = 0
    email_skipped_no_address = 0
    email_skipped_unconfigured = 0
    email_failed = 0
    email_configured = bool(settings.AZURE_CLIENT_ID and settings.EMAIL_SENDER)
    if not email_configured:
        logger.warning("Birthday emails will be skipped: set AZURE_CLIENT_ID and EMAIL_SENDER")

    for advisor_id, people in grouped.items():
        if not people:
            continue

        rm_email, rm_first_name = await _advisor_email_and_first_name(db, advisor_id)
        if not rm_email:
            email_skipped_no_address += 1
            logger.info(f"Birthday email skipped (no login email) advisor={advisor_id}")
            continue
        if not email_configured:
            email_skipped_unconfigured += 1
            continue

        emailed = await send_birthday_reminder_email(
            rm_email,
            people,
            rm_first_name=rm_first_name,
            reference_date=today,
        )
        if emailed:
            email_sent += 1
        else:
            email_failed += 1

    logger.info(
        f"Birthday email job finished slot={today.isoformat()} advisors={len(grouped)} "
        f"email_sent={email_sent} email_skipped_no_address={email_skipped_no_address} "
        f"email_skipped_unconfigured={email_skipped_unconfigured} email_failed={email_failed}"
    )
