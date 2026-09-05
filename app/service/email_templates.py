"""
Email content (subject/text/HTML) for outbound birthday reminder emails.
"""
from datetime import date
from html import escape
from typing import Optional

from app.schema.birthday import BirthdayPerson


def _birthday_person_subtitle(person: BirthdayPerson) -> str:
    if person.is_family_member:
        return f"{person.family_relationship or 'Family member'} of {person.household_client_name}"
    return "Primary client"


def format_birthday_person_line(person: BirthdayPerson) -> str:
    name = f"{person.first_name} {person.last_name}".strip()
    return f"{name} · {_birthday_person_subtitle(person)}"


def _format_subject_date(reference_date: date) -> str:
    return f"{reference_date.day} {reference_date.strftime('%B')}"


def _greeting(rm_first_name: Optional[str]) -> str:
    name = (rm_first_name or "").strip()
    return f"Hello {name}," if name else "Hello,"


def render_birthday_reminder_email(
    people: list[BirthdayPerson],
    *,
    rm_first_name: Optional[str] = None,
    reference_date: Optional[date] = None,
) -> tuple[str, str, str]:
    """Returns (subject, body_text, body_html) for an RM birthday reminder."""
    today = reference_date or date.today()
    date_label = _format_subject_date(today)
    greeting = _greeting(rm_first_name)
    lines = [format_birthday_person_line(person) for person in people]
    is_single = len(people) == 1

    if is_single:
        subject = f"Birthday of Client Today – {date_label}"
        intro = "The following client/family member has their birthday today:"
        list_text = lines[0] if lines else ""
        header_label = f"Birthday of Client Today · {date_label}"
    else:
        subject = f"Birthday of Clients Today – {date_label}"
        intro = "The following clients/family members have their birthday today:"
        list_text = "\n".join(f"{index}. {line}" for index, line in enumerate(lines, start=1))
        header_label = f"Birthday of Clients Today · {date_label}"

    closing = "Please consider wishing them on their special day."
    body_text = f"{greeting}\n\n{intro}\n\n{list_text}\n\n{closing}\n"

    if is_single and people:
        list_html = (
            f'<p style="margin: 0 0 24px 0; font-size: 16px; font-weight: 700; color: #1a1a2e; '
            f"line-height: 1.5; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;\">"
            f"{escape(lines[0])}</p>"
        )
    else:
        items = "".join(
            f'<p style="margin: 0 0 6px 0; font-size: 16px; color: #1a1a2e; line-height: 1.5;">'
            f"{index}. {escape(line)}</p>"
            for index, line in enumerate(lines, start=1)
        )
        list_html = f'<div style="margin: 0 0 24px 0;">{items}</div>'

    body_html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{escape(subject)}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f4f4f5;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" bgcolor="#f4f4f5" style="background-color: #f4f4f5;">
        <tr>
            <td align="center" style="padding: 24px 12px;">
                <table role="presentation" width="600" cellspacing="0" cellpadding="0" bgcolor="#ffffff" style="width: 600px; max-width: 600px; background-color: #ffffff;">
                    <tr>
                        <td bgcolor="#1a1a2e" align="center" style="padding: 28px 24px; background-color: #1a1a2e;">
                            <p style="margin: 0; font-size: 20px; font-weight: 700; color: #ffffff; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;">
                                Billion BaseCamp
                            </p>
                            <p style="margin: 8px 0 0 0; font-size: 14px; color: #fde68a; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;">
                                {escape(header_label)}
                            </p>
                        </td>
                    </tr>
                    <tr>
                        <td bgcolor="#ffffff" style="padding: 28px 24px; background-color: #ffffff; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;">
                            <p style="margin: 0 0 16px 0; font-size: 16px; color: #1a1a2e; line-height: 1.5;">
                                {escape(greeting)}
                            </p>
                            <p style="margin: 0 0 16px 0; font-size: 16px; color: #333333; line-height: 1.5;">
                                {escape(intro)}
                            </p>
                            {list_html}
                            <p style="margin: 0; font-size: 16px; color: #333333; line-height: 1.5;">
                                {escape(closing)}
                            </p>
                        </td>
                    </tr>
                    <tr>
                        <td bgcolor="#f8f9fa" align="center" style="padding: 16px 24px; background-color: #f8f9fa; border-top: 1px solid #eeeeee;">
                            <p style="margin: 0; font-size: 12px; color: #888888; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;">
                                &copy; Billion BaseCamp. All rights reserved.
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
"""
    return subject, body_text, body_html
