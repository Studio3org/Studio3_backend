"""The value in a ticket's QR code, and checking one.

A code is the ticket's id and its event's id, signed with Ed25519:

    S3T1.<base64url(version | ticket id | event id | signature)>

Signed, not random, so the door can tell a real ticket from a forged one; and signed with a
private key rather than a shared secret, so in Phase 4 a scanner can check codes offline with
only the **public** key, which is safe to hand to every co-host's phone. The same value goes
in the Google and Apple Wallet passes.

Nothing is stored: a code is made from the ticket's id whenever it is shown. Whether the
ticket still admits anyone (refunded, already scanned) is the ticket row's status, checked at
the door — the signature only proves it was issued by us for that event.

The key comes from TICKET_SIGNING_KEY (a base64 32-byte Ed25519 seed). Without it, one is
derived from SECRET_KEY, so development and staging work with no extra setup; set it
explicitly in production so rotating SECRET_KEY doesn't invalidate every ticket.
"""
from __future__ import annotations

import base64
import os
import uuid
from functools import lru_cache
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PREFIX = "S3T1."
_VERSION = b"\x01"
_PAYLOAD_LEN = 1 + 16 + 16
_SIG_LEN = 64


@lru_cache(maxsize=1)
def _private_key() -> Ed25519PrivateKey:
    explicit = (os.getenv("TICKET_SIGNING_KEY") or "").strip()
    if explicit:
        seed = base64.b64decode(explicit)
    else:
        secret = (os.getenv("SECRET_KEY") or "dev-secret-key-change-me").encode()
        seed = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=b"studio3-ticket-codes", info=b"ed25519"
        ).derive(secret)
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_b64() -> str:
    """What an offline scanner needs to check codes (Phase 4). Not a secret."""
    raw = _private_key().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_code(ticket_id: uuid.UUID, event_id: uuid.UUID) -> str:
    payload = _VERSION + ticket_id.bytes + event_id.bytes
    return PREFIX + _b64(payload + _private_key().sign(payload))


def read_code(code: str) -> Optional[tuple[uuid.UUID, uuid.UUID]]:
    """(ticket id, event id) for a genuine code, else None. Never raises on bad input."""
    if not code or not code.startswith(PREFIX):
        return None
    try:
        raw = _unb64(code[len(PREFIX):])
    except (ValueError, TypeError):
        return None
    if len(raw) != _PAYLOAD_LEN + _SIG_LEN or raw[:1] != _VERSION:
        return None
    payload, signature = raw[:_PAYLOAD_LEN], raw[_PAYLOAD_LEN:]
    try:
        _private_key().public_key().verify(signature, payload)
    except InvalidSignature:
        return None
    return uuid.UUID(bytes=payload[1:17]), uuid.UUID(bytes=payload[17:33])
