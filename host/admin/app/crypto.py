"""Symmetric encryption for at-rest secrets (e.g. GitHub PATs). Uses Fernet
keyed off the ENCRYPTION_KEY env var (32-byte hex, generated at host setup).

In a cluster, ENCRYPTION_KEY is cluster-scoped (every node shares it) so
encrypted blobs replicate cleanly between nodes. The sealed-box helpers below
are how the key itself travels: a joining node publishes an X25519 public key,
and an existing member seals the cluster secrets to it (ephemeral X25519 →
HKDF → AES-256-GCM), so neither the LAN nor the control plane sees plaintext.
"""

import base64
import binascii
import json
import os
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import settings


def _key_bytes(raw: str) -> bytes:
    try:
        key_bytes = binascii.unhexlify(raw)
    except binascii.Error:
        # Already base64? Try as-is.
        key_bytes = raw.encode("utf-8")
    if len(key_bytes) != 32:
        # Truncate or pad to 32 bytes deterministically.
        key_bytes = (key_bytes * (32 // max(len(key_bytes), 1) + 1))[:32]
    return key_bytes


def _fernet(raw: str | None = None) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_key_bytes(raw or settings.encryption_key)))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return ""


def encrypt_with(key: str, plaintext: str) -> str:
    """Encrypt with an explicit key (not the process-global one). Used during a
    cluster join, when secrets must be written under the incoming cluster key
    before the app restarts onto it."""
    return _fernet(key).encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_with(key: str, token: str) -> str:
    try:
        return _fernet(key).decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return ""


# ───── X25519 sealed boxes (cluster key exchange) ─────────────────────────────

_HKDF_INFO = b"homebox-cluster-sealed-box-v1"


def generate_keypair() -> tuple[str, str]:
    """Returns (private_hex, public_hex) for a fresh X25519 keypair."""
    priv = X25519PrivateKey.generate()
    priv_hex = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    ).hex()
    pub_hex = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    return priv_hex, pub_hex


def generate_wg_keypair() -> tuple[str, str]:
    """Returns (private_b64, public_b64) in WireGuard's key format. WG keys are
    Curve25519, so an X25519 keypair base64-encoded is exactly a wg key — no
    `wg genkey` binary needed."""
    priv = X25519PrivateKey.generate()
    priv_b64 = base64.b64encode(priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )).decode("ascii")
    pub_b64 = base64.b64encode(priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )).decode("ascii")
    return priv_b64, pub_b64


def _derive(shared: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(shared)


def seal_to(recipient_pub_hex: str, payload: dict) -> str:
    """Seal a JSON payload to a recipient's X25519 public key. Anonymous sender:
    an ephemeral keypair per message; its public half rides along."""
    recipient = X25519PublicKey.from_public_bytes(bytes.fromhex(recipient_pub_hex))
    eph = X25519PrivateKey.generate()
    key = _derive(eph.exchange(recipient))
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, json.dumps(payload).encode("utf-8"), None)
    eph_pub = eph.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(eph_pub + nonce + ct).decode("ascii")


def unseal(private_hex: str, sealed: str) -> dict:
    """Open a payload sealed to our public key. Raises on tamper/wrong key."""
    blob = base64.b64decode(sealed)
    eph_pub, nonce, ct = blob[:32], blob[32:44], blob[44:]
    priv = X25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    key = _derive(priv.exchange(X25519PublicKey.from_public_bytes(eph_pub)))
    return json.loads(AESGCM(key).decrypt(nonce, ct, None))
