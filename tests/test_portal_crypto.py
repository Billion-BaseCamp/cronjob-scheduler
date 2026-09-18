from cryptography.fernet import Fernet

from app.core.config import settings
from app.portal.crypto import (
    PortalSecretDecryptError,
    decrypt_portal_secret,
    looks_like_fernet_token,
)


def test_looks_like_fernet_token() -> None:
    key = Fernet.generate_key()
    token = Fernet(key).encrypt(b"secret").decode()
    assert looks_like_fernet_token(token) is True
    assert looks_like_fernet_token("plaintext-password") is False
    assert looks_like_fernet_token("gAAAAAnot-valid") is False


def test_decrypt_roundtrip(monkeypatch) -> None:
    key = Fernet.generate_key().decode()
    token = Fernet(key.encode()).encrypt(b"portal-pass").decode()
    monkeypatch.setattr(settings, "DOCUMENT_PASSWORD_ENCRYPTION_KEY", key)
    assert decrypt_portal_secret(token) == "portal-pass"


def test_wrong_key_raises_and_does_not_return_ciphertext(monkeypatch) -> None:
    key = Fernet.generate_key().decode()
    other = Fernet.generate_key().decode()
    token = Fernet(key.encode()).encrypt(b"portal-pass").decode()
    monkeypatch.setattr(settings, "DOCUMENT_PASSWORD_ENCRYPTION_KEY", other)
    try:
        decrypt_portal_secret(token)
        raise AssertionError("expected PortalSecretDecryptError")
    except PortalSecretDecryptError:
        pass


def test_legacy_plaintext_passthrough(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "DOCUMENT_PASSWORD_ENCRYPTION_KEY",
        Fernet.generate_key().decode(),
    )
    assert decrypt_portal_secret("not-a-token") == "not-a-token"
