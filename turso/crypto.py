"""Secret handling: Fernet for stored Turso JWTs, hashing for our API tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet

from turso import config

TOKEN_PREFIX = "dbc"
RAW_TOKEN_LENGTH = 43  # secrets.token_urlsafe(32) -> 43 url-safe chars


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(config.TURSO_ENC_KEY.encode())


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a Turso JWT for storage."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()


def generate_api_token() -> str:
    """Our customer-facing token: ``dbc_<43 url-safe chars>``."""
    return f"{TOKEN_PREFIX}_{secrets.token_urlsafe(32)}"


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def token_prefix(raw_token: str) -> str:
    """First 8 chars of the raw token, safe to show in listings."""
    return raw_token[:8]


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
