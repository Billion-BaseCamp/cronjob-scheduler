"""
Send emails via Microsoft Graph API (Azure AD + MSAL).
Requires AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID, and EMAIL_SENDER.
"""
from typing import Optional

import msal
import requests

from app.core.config import settings

GRAPH_SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/users/{user_id}/sendMail"
SCOPE = ["https://graph.microsoft.com/.default"]


class AzureEmailClientError(Exception):
    """Raised when Azure email client is misconfigured or send fails."""

    pass


class AzureEmailClient:
    """Send emails via Microsoft Graph API using app-only (client credentials) flow."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        tenant_id: Optional[str] = None,
        sender_email: Optional[str] = None,
    ):
        self.client_id = client_id or settings.AZURE_CLIENT_ID
        self.client_secret = client_secret or settings.AZURE_CLIENT_SECRET
        self.tenant_id = tenant_id or settings.AZURE_TENANT_ID
        self.sender_email = sender_email or settings.EMAIL_SENDER
        self._app: Optional[msal.ConfidentialClientApplication] = None

    def _get_app(self) -> msal.ConfidentialClientApplication:
        if not all([self.client_id, self.client_secret, self.tenant_id]):
            raise AzureEmailClientError(
                "Azure email is not configured: set AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID"
            )
        if self._app is None:
            authority = f"https://login.microsoftonline.com/{self.tenant_id}"
            self._app = msal.ConfidentialClientApplication(
                self.client_id,
                authority=authority,
                client_credential=self.client_secret,
            )
        return self._app

    def _get_access_token(self) -> str:
        app = self._get_app()
        result = app.acquire_token_for_client(scopes=SCOPE)
        if "access_token" not in result:
            error = result.get("error_description") or result.get("error", "Unknown token error")
            raise AzureEmailClientError(f"Failed to acquire token: {error}")
        return result["access_token"]

    def send_mail(
        self,
        to_email: str,
        subject: str,
        body_text: str,
        *,
        body_html: Optional[str] = None,
        sender: Optional[str] = None,
    ) -> None:
        sender_id = sender or self.sender_email
        if not sender_id:
            raise AzureEmailClientError(
                "EMAIL_SENDER is not set; cannot send email via Graph API"
            )

        use_html = bool(body_html)
        access_token = self._get_access_token()
        url = GRAPH_SEND_MAIL_URL.format(user_id=sender_id)
        payload = {
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "HTML" if use_html else "Text",
                    "content": body_html if use_html else body_text,
                },
                "toRecipients": [
                    {"emailAddress": {"address": to_email}},
                ],
            }
        }
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if response.status_code != 202:
            raise AzureEmailClientError(
                f"Graph API sendMail failed: {response.status_code} {response.text}"
            )


_client: Optional[AzureEmailClient] = None


def get_azure_email_client() -> AzureEmailClient:
    global _client
    if _client is None:
        _client = AzureEmailClient()
    return _client
