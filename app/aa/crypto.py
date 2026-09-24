"""Column-level encryption for stored financial data.

Uses Fernet, the same primitive as ``tax-engine-backend``'s document-password
helper, but with a **separate key**. Portal passwords and full bank statements
have different blast radii; rotating one must not force rotating the other.

RDS disk encryption protects against a stolen disk. It does not protect against
a leaked query result, an over-broad admin read, or a backup restored into the
wrong place — which is what these columns defend.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)


def _fernet() -> Fernet:
    key = settings.AA_ENCRYPTION_KEY.strip()
    if not key:
        raise RuntimeError(
            "AA_ENCRYPTION_KEY is not set — refusing to handle financial data."
        )
    return Fernet(key.encode("utf-8"))


def encrypt_text(plaintext: Optional[str]) -> Optional[bytes]:
    if plaintext is None:
        return None
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_text(ciphertext: Optional[bytes]) -> Optional[str]:
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext).decode("utf-8")
    except InvalidToken:
        # Wrong key or corrupted value. Never log the ciphertext.
        logger.error("Failed to decrypt an AA column — key mismatch or corruption")
        return None


def encrypt_json(value: Any) -> Optional[bytes]:
    """Serialise then encrypt. ``default=str`` keeps Decimal/datetime safe."""
    if value is None:
        return None
    return encrypt_text(json.dumps(value, default=str, separators=(",", ":")))


def decrypt_json(ciphertext: Optional[bytes]) -> Optional[Any]:
    raw = decrypt_text(ciphertext)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Decrypted AA column was not valid JSON")
        return None


def generate_key() -> str:
    """Helper for provisioning: ``python -c "from app.security.crypto import generate_key; print(generate_key())"``."""
    return Fernet.generate_key().decode("utf-8")
