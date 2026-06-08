"""Fernet encryption for broker credentials (api_secret, password, TOTP secret).

Plaintext credentials never touch the database or git. The Fernet key comes from
``SKAS_SECRET_ENCRYPTION_KEY`` (env / .env, git-ignored). Generate one with
``generate_key()`` or the CLI snippet in .env.example.
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from skas_algo.config import get_settings


class EncryptionKeyMissing(RuntimeError):
    """Raised when encryption is attempted without SKAS_SECRET_ENCRYPTION_KEY set."""


def generate_key() -> str:
    """Return a new url-safe base64 Fernet key."""
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    key = get_settings().secret_encryption_key
    if not key:
        raise EncryptionKeyMissing(
            "SKAS_SECRET_ENCRYPTION_KEY is not set. Generate one with "
            "`python -c 'from skas_algo.security import generate_key; print(generate_key())'`."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str | None) -> str | None:
    """Encrypt a string; passes through None/empty for optional fields."""
    if plaintext is None or plaintext == "":
        return None
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str | None) -> str | None:
    if ciphertext is None or ciphertext == "":
        return None
    return _fernet().decrypt(ciphertext.encode()).decode()
