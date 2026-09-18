"""Decrypt client.it_portal_pass with the same Fernet key as tax-engine."""

from __future__ import annotations

import base64
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)

# Fernet: version (0x80) + timestamp + IV + ciphertext + HMAC. 57 bytes min.
_FERNET_MIN_BYTES = 57
_FERNET_VERSION = b"\x80"


class PortalSecretDecryptError(Exception):
    """Stored value is Fernet ciphertext but this process cannot decrypt it."""


def looks_like_fernet_token(value: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception:
        return False
    return len(raw) >= _FERNET_MIN_BYTES and raw[:1] == _FERNET_VERSION


def decrypt_portal_secret(ciphertext: Optional[str]) -> Optional[str]:
    if ciphertext is None or ciphertext == "":
        return None
    key = settings.DOCUMENT_PASSWORD_ENCRYPTION_KEY.strip()
    if not key:
        raise RuntimeError("DOCUMENT_PASSWORD_ENCRYPTION_KEY is not configured")
    try:
        f = Fernet(key.encode("utf-8"))
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        if looks_like_fernet_token(ciphertext):
            logger.error(
                "Portal secret decrypt failed — wrong Fernet key or corrupt token"
            )
            raise PortalSecretDecryptError(
                "DOCUMENT_PASSWORD_ENCRYPTION_KEY does not match stored ciphertext"
            ) from None
        return ciphertext
