from datetime import date
from typing import Optional

from app.clients.azure_email_client import AzureEmailClientError, get_azure_email_client
from app.core.config import settings
from app.core.logger import logger
from app.schema.birthday import BirthdayPerson
from app.service.email_templates import render_birthday_reminder_email


async def send_birthday_reminder_email(
    to_email: str,
    people: list[BirthdayPerson],
    *,
    rm_first_name: Optional[str] = None,
    reference_date: Optional[date] = None,
) -> bool:
    """
    Send one birthday reminder email to an RM.

    Returns True if Graph accepted the send.
    Returns False if Azure is not configured, the recipient/people are missing,
    or Graph fails (failures are logged and not raised).
    """
    if not to_email or not people:
        logger.debug(f"Birthday email skipped: missing recipient or people to={to_email}")
        return False

    if not settings.AZURE_CLIENT_ID or not settings.EMAIL_SENDER:
        logger.debug(
            f"Birthday email skipped (AZURE_CLIENT_ID/EMAIL_SENDER not set); to={to_email}"
        )
        return False

    try:
        client = get_azure_email_client()
        subject, body_text, body_html = render_birthday_reminder_email(
            people,
            rm_first_name=rm_first_name,
            reference_date=reference_date,
        )
        client.send_mail(to_email, subject, body_text, body_html=body_html)
        logger.info(f"Birthday reminder email sent to={to_email} people={len(people)}")
        return True
    except AzureEmailClientError as e:
        if "not configured" in str(e).lower():
            logger.debug(f"Birthday email skipped ({e}); to={to_email}")
            return False
        logger.exception(f"Birthday email failed (Azure): {e} to={to_email}")
        return False
    except Exception as e:
        logger.exception(f"Birthday email failed: {e} to={to_email}")
        return False
