"""Decrypt client.it_portal_pass with the same Fernet key as tax-engine."""

from __future__ import annotations

import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)


def decrypt_portal_secret(ciphertext: Optional[str]) -> Optional[str]:
    if ciphertext is None or ciphertext == "":
        return None
    key = settings.DOCUMENT_PASSWORD_ENCRYPTION_KEY.strip()
    if not key:
        raise RuntimeError("DOCUMENT_PASSWORD_ENCRYPTION_KEY is not configured")
    f = Fernet(key.encode("utf-8"))
    try:
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return ciphertext
