"""Symmetric encryption for at-rest secrets (e.g. GitHub PATs). Uses Fernet
keyed off the ENCRYPTION_KEY env var (32-byte hex, generated at host setup)."""

import base64
import binascii
from cryptography.fernet import Fernet, InvalidToken

from .config import settings


def _fernet() -> Fernet:
    raw = settings.encryption_key
    try:
        key_bytes = binascii.unhexlify(raw)
    except binascii.Error:
        # Already base64? Try as-is.
        key_bytes = raw.encode("utf-8")
    if len(key_bytes) != 32:
        # Truncate or pad to 32 bytes deterministically.
        key_bytes = (key_bytes * (32 // max(len(key_bytes), 1) + 1))[:32]
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return ""
