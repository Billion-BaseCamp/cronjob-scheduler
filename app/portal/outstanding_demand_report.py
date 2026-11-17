"""Two outstanding-demand reports per RM when the monthly batch finishes.

A scheduled check does not email on its own. Once every job in the batch has
settled, each RM gets at most two mails, each with a workbook of that RM's
clients: demands still needing Submit Response, and demands where a response
is already filed. A mail is skipped when that RM has no rows for it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from io import BytesIO
from typing import Any, Mapping, Optional
from uuid import UUID

from sqlalchemy import select

from app.portal.notice_alerts import (
    CRON_SILENT_REQUESTED_BY,
    DEMAND_REPORT_ACTION,
    DEMAND_REPORT_RESPONDED,
    XLSX_CONTENT_TYPE,
    client_display_name,
    outstanding_demand_report_filename,
    render_outstanding_demand_report,
)
from app.portal.notice_diagnosis import notice_needs_action
from app.portal.notice_schedule import RUN_OUTSTANDING_DEMAND_MONTHLY

logger = logging.getLogger(__name__)

_COLUMNS = (
    "Client",
    "PAN",
    "AY",
    "Section",
    "DIN",
    "Raised on",
    "Outstanding",
    "Interest",
    "Status",
    "Response type",
    "AO response",
    "Rectification",
    "Act",
)
_WIDTHS = (28, 14, 12, 16, 20, 14, 16, 14, 22, 28, 28, 22, 28)


@dataclass
class ReportClient:
    name: str
    pan: Optional[str]
    action: tuple[Mapping[str, Any], ...]
    responded: tuple[Mapping[str, Any], ...]


@dataclass
class AdvisorReports:
    email: str
    first_name: Optional[str]
    clients: list[ReportClient]


def should_send_demand_reports(run_type: Optional[str], requested_by: Optional[str]) -> bool:
    return (
        run_type == RUN_OUTSTANDING_DEMAND_MONTHLY
        and (requested_by or "") != CRON_SILENT_REQUESTED_BY
    )


def _response_was_filed(notice: Mapping[str, Any]) -> bool:
    """A harvested demand that is not waiting on Submit Response."""
    if notice.get("is_extinguished"):
        return True
    if notice.get("response_type") or notice.get("response_submitted_on"):
        return True
    actions = notice.get("actions")
    if not isinstance(actions, dict) or actions.get("submit_response"):
        return False
    return bool(actions)


def _amount_key(notice: Mapping[str, Any]) -> tuple:
    try:
        amount = float(notice.get("outstanding_demand_amount"))
    except (TypeError, ValueError):
        amount = 0.0
    return (-amount, str(notice.get("din") or ""))


def bucket_demands(
    notices: list[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split one client's harvest into (action required, response already filed)."""
    action: list[dict[str, Any]] = []
    responded: list[dict[str, Any]] = []
    for notice in notices or []:
        if not isinstance(notice, Mapping):
            continue
        row = dict(notice)
        if notice_needs_action(row):
            action.append(row)
        elif _response_was_filed(row):
            responded.append(row)
    action.sort(key=_amount_key)
    responded.sort(key=_amount_key)
    return action, responded


def _iso_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _excel_amount(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _interest(notice: Mapping[str, Any]) -> Any:
    interest = notice.get("accrued_interest")
    if interest is None:
        interest = notice.get("final_interest")
    return interest


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def demand_workbook(clients: list[Mapping[str, Any]]) -> bytes:
    """One sheet, one row per demand. PAN is the full value so the RM can look the client up."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Demands"
    sheet.append(list(_COLUMNS))
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for client in clients:
        name = client.get("name") or "Unnamed client"
        pan = (client.get("pan") or "").strip().upper() or None
        for notice in client.get("notices") or []:
            sheet.append(
                [
                    name,
                    pan,
                    _text(notice.get("assessment_year")),
                    _text(notice.get("notice_section")),
                    _text(notice.get("din")),
                    _iso_date(notice.get("date_of_demand_raised")),
                    _excel_amount(notice.get("outstanding_demand_amount")),
                    _excel_amount(_interest(notice)),
                    _text(notice.get("current_status")),
                    _text(notice.get("response_type")),
                    _text(notice.get("ao_response")),
                    _text(notice.get("rectification_rights")),
                    _text(notice.get("applicable_act")),
                ]
            )
    sheet.auto_filter.ref = sheet.dimensions
    sheet.freeze_panes = "A2"
    for index, width in enumerate(_WIDTHS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for row in sheet.iter_rows(min_row=2, min_col=6, max_col=6):
        for cell in row:
            if isinstance(cell.value, date):
                cell.number_format = "DD-MMM-YYYY"
    for row in sheet.iter_rows(min_row=2, min_col=7, max_col=8):
        for cell in row:
            if isinstance(cell.value, int):
                cell.number_format = "#,##0"
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _views(clients: list[ReportClient], attr: str) -> list[dict[str, Any]]:
    views = []
    for client in clients:
        notices = getattr(client, attr)
        if notices:
            views.append({"name": client.name, "pan": client.pan, "notices": list(notices)})
    views.sort(key=lambda row: row["name"].casefold())
    return views


async def deliver_demand_reports(
    advisors: list[AdvisorReports],
    *,
    checked_at: datetime,
    send,
) -> dict[str, int]:
    """Send whichever of the two reports an RM actually has rows for."""
    counts = {"sent_action": 0, "sent_responded": 0, "failed": 0}
    for advisor in advisors:
        if not (advisor.email or "").strip():
            continue
        for kind, attr, key in (
            (DEMAND_REPORT_ACTION, "action", "sent_action"),
            (DEMAND_REPORT_RESPONDED, "responded", "sent_responded"),
        ):
            views = _views(advisor.clients, attr)
            if not views:
                continue
            filename = outstanding_demand_report_filename(kind, checked_at)
            subject, text, html = render_outstanding_demand_report(
                kind=kind,
                advisor_first_name=advisor.first_name,
                clients=views,
                filename=filename,
                checked_at=checked_at,
            )
            try:
                sent = await send(
                    advisor.email,
                    subject,
                    text,
                    html,
                    (filename, XLSX_CONTENT_TYPE, demand_workbook(views)),
                )
            except Exception:
                logger.exception("Outstanding demand report failed for %s", advisor.email)
                sent = False
            if sent:
                counts[key] += 1
            else:
                counts["failed"] += 1
    return counts


async def _send(to_email: str, subject: str, text: str, html: str, attachment) -> bool:
    from app.clients.azure_email_client import AzureEmailClientError, get_azure_email_client
    from app.core.config import settings

    if not to_email:
        return False
    if not settings.AZURE_CLIENT_ID or not settings.EMAIL_SENDER:
        logger.info("Outstanding demand report skipped: Azure email is not configured")
        return False
    filename, content_type, content = attachment
    try:
        get_azure_email_client().send_mail(
            to_email,
            subject,
            text,
            body_html=html,
            attachments=[(filename, content_type, content)],
        )
        return True
    except AzureEmailClientError:
        logger.exception("Outstanding demand report failed for %s", to_email)
        return False
    except Exception:
        logger.exception("Outstanding demand report failed for %s", to_email)
        return False


async def _advisors_for_batch(db, batch_id: UUID) -> tuple[list[AdvisorReports], dict[str, int]]:
    from nucleus.models import Client
    from nucleus.models.common_models.advisor import Advisor
    from nucleus.models.common_models.login import Login
    from nucleus.models.portal_automation import PortalAutomationJob

    rows = (
        await db.execute(
            select(PortalAutomationJob.result, Client)
            .join(Client, Client.id == PortalAutomationJob.client_id)
            .where(
                PortalAutomationJob.batch_id == batch_id,
                PortalAutomationJob.status == "completed",
            )
        )
    ).all()

    parent_ids = {
        client.parent_id
        for _result, client in rows
        if client.advisor_id is None and client.parent_id is not None
    }
    parents: dict[UUID, Any] = {}
    if parent_ids:
        parent_rows = (
            await db.execute(select(Client).where(Client.id.in_(parent_ids)))
        ).scalars().all()
        parents = {parent.id: parent for parent in parent_rows}

    classified: list[tuple[UUID, ReportClient]] = []
    skipped_no_advisor = 0
    for result, client in rows:
        notices = result.get("notices") if isinstance(result, dict) else None
        action, responded = bucket_demands(notices)
        if not action and not responded:
            continue
        advisor_id = client.advisor_id
        if advisor_id is None and client.parent_id is not None:
            parent = parents.get(client.parent_id)
            advisor_id = getattr(parent, "advisor_id", None) if parent else None
        if advisor_id is None:
            skipped_no_advisor += 1
            continue
        classified.append(
            (
                advisor_id,
                ReportClient(
                    name=client_display_name(client),
                    pan=getattr(client, "pan_number", None),
                    action=tuple(action),
                    responded=tuple(responded),
                ),
            )
        )

    contacts: dict[UUID, tuple[str, Optional[str]]] = {}
    advisor_ids = {advisor_id for advisor_id, _client in classified}
    if advisor_ids:
        contact_rows = (
            await db.execute(
                select(Login.advisor_id, Login.email, Advisor.first_name)
                .join(Advisor, Advisor.id == Login.advisor_id)
                .where(
                    Login.advisor_id.in_(advisor_ids),
                    Login.email.isnot(None),
                    Login.email.like("%@%"),
                )
            )
        ).all()
        for advisor_id, email, first_name in contact_rows:
            address = (email or "").strip()
            if advisor_id not in contacts and address:
                contacts[advisor_id] = (address, (first_name or "").strip() or None)

    grouped: dict[UUID, AdvisorReports] = {}
    skipped_no_email = 0
    seen_without_email: set[UUID] = set()
    for advisor_id, report_client in classified:
        contact = contacts.get(advisor_id)
        if contact is None:
            if advisor_id not in seen_without_email:
                skipped_no_email += 1
                seen_without_email.add(advisor_id)
            continue
        email, first_name = contact
        bundle = grouped.get(advisor_id)
        if bundle is None:
            bundle = AdvisorReports(email=email, first_name=first_name, clients=[])
            grouped[advisor_id] = bundle
        bundle.clients.append(report_client)
    return (
        list(grouped.values()),
        {"no_advisor": skipped_no_advisor, "no_email": skipped_no_email},
    )


async def send_outstanding_demand_reports(
    batch_id: UUID,
    *,
    checked_at: Optional[datetime] = None,
) -> dict[str, int]:
    """Email each RM in a finished monthly batch. Failures are logged, not raised."""
    from app.db.database import AsyncSessionLocal

    checked_at = checked_at or datetime.now(timezone.utc)
    try:
        async with AsyncSessionLocal() as db:
            advisors, skipped = await _advisors_for_batch(db, batch_id)
    except Exception:
        logger.exception("Could not load outstanding demand reports for %s", batch_id)
        return {"sent_action": 0, "sent_responded": 0, "failed": 0, "skipped_no_advisor": 0, "skipped_no_email": 0}

    counts = await deliver_demand_reports(advisors, checked_at=checked_at, send=_send)
    counts.update(skipped)
    logger.info("Outstanding demand reports batch=%s %s", batch_id, counts)
    return counts
