"""The only module that talks to Finsense.

Every shape here was verified against the live UAT endpoint on 2026-09-17, not
taken from the PDF. Where the documentation and reality disagree, reality won:

* ``POST /ConsentRequestPlus`` returns ``ConsentHandle`` — **capital C** —
  alongside lowercase ``url``, ``requestDate``, ``encryptedRequest``. A client
  reading ``consentHandle`` silently gets ``None``.
* ``GET /ConsentStatus/{handle}/{custId}`` has **no** ``/sync`` segment and the
  custId carries no ``@finvu`` suffix in that path (the PDF shows both).
* Errors arrive as HTTP 400 with an ``errors[]`` array of
  ``{errorCode, errorMsg}`` rather than an exception-shaped body, and an
  ``errors`` key can appear on a 200 as well — so it is checked on every call.
* ``dhanaprayoga`` and ``quantmutual`` resolve to the same IP; the host
  difference between the two documents is cosmetic.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.aa.token import (
    FinsenseAuthError,
    FinsenseTokenManager,
    build_header,
)
from app.core.config import settings

logger = logging.getLogger(__name__)


class FinsenseError(RuntimeError):
    """A business-level error returned by Finsense."""

    def __init__(self, message: str, *, code: Optional[str] = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        # Distinguishing retryable from terminal matters: retrying a rejected
        # consent forever would hammer the vendor and risk rate-limiting the
        # whole channel.
        self.retryable = retryable


class FinsenseClient:
    def __init__(self, token_manager: FinsenseTokenManager) -> None:
        self._tokens = token_manager
        self._base = settings.FINSENSE_BASE_URL

    # ------------------------------------------------------------------ core
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        rid: Optional[str] = None,
        _retried_auth: bool = False,
    ) -> dict[str, Any]:
        token = await self._tokens.get_token()
        url = f"{self._base}{path}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        async with httpx.AsyncClient(timeout=settings.FINSENSE_TIMEOUT_SECONDS) as client:
            resp = await client.request(method, url, json=json_body, headers=headers)

        # An expired or revoked token: refresh once, then give up. Retrying
        # indefinitely against a rate-limited login is how a channel gets
        # locked out.
        if resp.status_code == 401 and not _retried_auth:
            logger.warning("Finsense returned 401 on %s — refreshing token once", path)
            await self._tokens.invalidate()
            return await self._request(
                method, path, json_body=json_body, rid=rid, _retried_auth=True
            )

        if resp.status_code >= 500:
            raise FinsenseError(
                f"Finsense {method} {path} failed with HTTP {resp.status_code}",
                retryable=True,
            )

        try:
            data: dict[str, Any] = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise FinsenseError(
                f"Finsense {method} {path} returned non-JSON (HTTP {resp.status_code})",
                retryable=resp.status_code >= 500,
            ) from exc

        # Checked on every response, including 200s.
        errors = data.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) and errors else {}
            code = str(first.get("errorCode", resp.status_code))
            msg = first.get("errorMsg", "unknown Finsense error")
            raise FinsenseError(msg, code=code, retryable=resp.status_code >= 500)

        if resp.status_code >= 400:
            raise FinsenseError(
                f"Finsense {method} {path} failed with HTTP {resp.status_code}",
                retryable=False,
            )

        return data

    # --------------------------------------------------------------- consent
    async def request_consent(
        self,
        *,
        cust_id: str,
        user_session_id: str,
        rid: str,
        template_name: Optional[str] = None,
        redirect_url: Optional[str] = None,
        consent_description: str = "PFM",
    ) -> dict[str, Any]:
        """Raise a consent journey. Returns ``{consent_handle, journey_url, raw}``."""
        payload = {
            "header": build_header(rid),
            "body": {
                "custId": cust_id,
                "consentDescription": consent_description,
                "templateName": template_name or settings.FINSENSE_TEMPLATE_BANK,
                "redirectUrl": redirect_url or settings.FINSENSE_REDIRECT_URL,
                "userSessionId": user_session_id,
                "aaId": settings.FINSENSE_AA_ID,
            },
        }
        data = await self._request("POST", "/ConsentRequestPlus", json_body=payload, rid=rid)
        body = data.get("body") or {}

        # Capital C — verified live. Lowercase fallback in case the vendor
        # normalises it later.
        handle = body.get("ConsentHandle") or body.get("consentHandle")
        if not handle:
            raise FinsenseError(
                f"ConsentRequestPlus returned no ConsentHandle (keys={sorted(body)})"
            )
        return {
            "consent_handle": handle,
            "journey_url": body.get("url"),
            "raw": data,
        }

    async def consent_status(self, *, consent_handle: str, cust_id: str) -> dict[str, Any]:
        """Poll consent state. The GET fallback when a callback never arrives.

        ``cust_id`` here is the bare mobile number, without the ``@finvu``
        suffix — verified against the live endpoint.
        """
        return await self._request(
            "GET", f"/ConsentStatus/{consent_handle}/{cust_id}"
        )

    async def consent_details(self, *, consent_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/Consent/{consent_id}")

    async def revoke_consent(self, *, consent_id: str, cust_id: str) -> dict[str, Any]:
        payload = {
            "header": build_header(),
            "body": {"consentId": consent_id, "custId": cust_id},
        }
        return await self._request("POST", "/ConsentRevoke", json_body=payload)

    # ------------------------------------------------------------------- data
    async def fi_request(
        self,
        *,
        cust_id: str,
        consent_handle: str,
        consent_id: str,
        date_from: str,
        date_to: str,
    ) -> dict[str, Any]:
        """Ask for data. Returns ``{session_id, txn_id, raw}``.

        Built unconditionally even though Auto-FI can skip it: with a periodic
        consent Auto-FI only covers the first fetch, so refreshes always land
        here.
        """
        payload = {
            "header": build_header(),
            "body": {
                "custId": cust_id,
                "consentHandleId": consent_handle,
                "consentId": consent_id,
                "dateTimeRangeFrom": date_from,
                "dateTimeRangeTo": date_to,
            },
        }
        data = await self._request("POST", "/FIRequest", json_body=payload)
        body = data.get("body") or {}
        session_id = body.get("sessionId") or body.get("SessionId")
        if not session_id:
            raise FinsenseError(
                f"FIRequest returned no sessionId (keys={sorted(body)})", retryable=True
            )
        return {
            "session_id": session_id,
            "txn_id": body.get("txnid") or body.get("txnId"),
            "raw": data,
        }

    async def fi_status(
        self,
        *,
        consent_id: str,
        session_id: str,
        consent_handle: str,
        cust_id: str,
    ) -> dict[str, Any]:
        return await self._request(
            "GET", f"/FIStatus/{consent_id}/{session_id}/{consent_handle}/{cust_id}"
        )

    async def fi_data_fetch(self, *, consent_handle: str, session_id: str) -> dict[str, Any]:
        """Fetch the actual financial data.

        Path verified live: ``/FIDataFetch/{handle}/{sessionId}`` — the PDF's
        ``/FIFetch/{custId}/{consentId}/{sessionId}`` is wrong for this channel.
        """
        return await self._request(
            "GET", f"/FIDataFetch/{consent_handle}/{session_id}"
        )

    # ---------------------------------------------------------------- utility
    async def webview_decrypt(
        self, *, encrypted_request: str, request_date: str, encrypted_fiu_id: str
    ) -> dict[str, Any]:
        """Decode the redirect payload the browser returns with.

        ``status == "S"`` means the *journey* finished — not that data exists.
        The webhook remains the authoritative signal.
        """
        payload = {
            "header": build_header(),
            "body": {
                "encryptedRequest": encrypted_request,
                "requestDate": request_date,
                "encryptedFiuId": encrypted_fiu_id,
                "aaId": settings.FINSENSE_AA_ID,
            },
        }
        return await self._request("POST", "/Webview/Decrypt", json_body=payload)


__all__ = ["FinsenseClient", "FinsenseError", "FinsenseAuthError"]
