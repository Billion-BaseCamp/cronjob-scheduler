"""Finsense channel token ("Token B") cache.

This token authenticates **Billion BaseCamp as an FIU**, not a user. Anyone
holding it can raise consents or fetch data for *any* of our customers, so it
lives server-side only: never serialised into a response, a log line or an
error message.

Two properties matter here.

**Refresh is locked, not just cached.** The vendor rate-limits ``/User/Login``
and returns ``401 Invalid Credentials`` when called too often. With aa-backend
and the worker both refreshing, a naive "refresh when expired" races: several
processes log in simultaneously and can lock the channel out of the entire AA
network. A Redis ``SET NX`` lock serialises it.

**Expiry is read from the token, not assumed.** The UAT token is a JWT whose
``exp`` claim measured exactly 24h. Reading it means we stay correct if
Finfactor changes the lifetime; the configured TTL is only a fallback for a
non-JWT token.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_TOKEN_KEY = "aa:finsense:token"
_LOCK_KEY = "aa:finsense:token:lock"


def utc_ts() -> str:
    """Finsense timestamp format: ISO-8601, milliseconds, explicit offset."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00"


def build_header(rid: Optional[str] = None) -> dict[str, str]:
    """Every Finsense call carries this header envelope."""
    return {
        "rid": rid or str(uuid.uuid4()),
        "ts": utc_ts(),
        "channelId": settings.FINSENSE_CHANNEL_ID,
    }


def _jwt_exp(token: str) -> Optional[int]:
    """Read the ``exp`` claim without verifying the signature.

    We are not authenticating this token — the vendor already did. We only want
    its expiry so the cache TTL matches reality.
    """
    if token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        return int(exp) if exp is not None else None
    except Exception:  # noqa: BLE001
        return None


class FinsenseTokenManager:
    """Fetches and caches the channel bearer token."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

    async def get_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = await self._redis.get(_TOKEN_KEY)
            if cached:
                return cached.decode() if isinstance(cached, bytes) else cached

        # Serialise refresh across every process.
        lock_id = str(uuid.uuid4())
        acquired = await self._redis.set(
            _LOCK_KEY, lock_id, nx=True, ex=settings.AA_TOKEN_LOCK_TIMEOUT_SECONDS
        )
        if not acquired:
            # Someone else is refreshing. Wait for their result rather than
            # issuing a competing login.
            token = await self._await_refresh()
            if token:
                return token
            logger.warning("token refresh wait timed out; refreshing directly")

        try:
            return await self._login_and_cache()
        finally:
            # Only release a lock we still own.
            current = await self._redis.get(_LOCK_KEY)
            current = current.decode() if isinstance(current, bytes) else current
            if current == lock_id:
                await self._redis.delete(_LOCK_KEY)

    async def _await_refresh(self, timeout: float = 15.0) -> Optional[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            cached = await self._redis.get(_TOKEN_KEY)
            if cached:
                return cached.decode() if isinstance(cached, bytes) else cached
        return None

    async def _login_and_cache(self) -> str:
        settings.require_finsense()
        url = f"{settings.FINSENSE_BASE_URL}/User/Login"
        payload = {
            "header": build_header(),
            "body": {
                "userId": settings.FINSENSE_USER_ID,
                "password": settings.FINSENSE_PASSWORD,
            },
        }

        async with httpx.AsyncClient(timeout=settings.FINSENSE_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload)

        if resp.status_code != 200:
            # Never echo the response body: it may contain the submitted
            # credentials back.
            logger.error("Finsense login failed: HTTP %s", resp.status_code)
            raise FinsenseAuthError(
                f"Finsense login failed with HTTP {resp.status_code}"
            )

        data: dict[str, Any] = resp.json()
        token = (data.get("body") or {}).get("token")
        if not token:
            logger.error("Finsense login returned no token; keys=%s", sorted(data))
            raise FinsenseAuthError("Finsense login returned no token")

        ttl = settings.AA_TOKEN_FALLBACK_TTL_SECONDS
        exp = _jwt_exp(token)
        if exp:
            remaining = exp - int(time.time()) - settings.AA_TOKEN_REFRESH_MARGIN_SECONDS
            if remaining > 0:
                ttl = remaining
        await self._redis.set(_TOKEN_KEY, token, ex=ttl)
        logger.info("Finsense channel token refreshed (cached for %ss)", ttl)
        return token

    async def invalidate(self) -> None:
        """Drop the cached token so the next call re-logs in.

        Called when the vendor returns 401 mid-flight: the token expired early
        or was revoked, and retrying with it would fail identically.
        """
        await self._redis.delete(_TOKEN_KEY)


class FinsenseAuthError(RuntimeError):
    """Raised when the channel cannot authenticate with Finsense."""
