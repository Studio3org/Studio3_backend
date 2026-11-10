"""Which wallets are set up, and the credentials they sign with.

Each wallet is optional: without its settings it reports itself unavailable
(`walletPasses.apple/google: false`) and nothing else changes. Credentials are parsed once and
cached; a malformed one makes that wallet unavailable and is logged, never a crash.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from src.shared.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class AppleCredentials:
    pass_type_id: str
    team_id: str
    certificate: object  # cryptography x509.Certificate
    private_key: object
    wwdr: object


@lru_cache(maxsize=1)
def apple() -> Optional[AppleCredentials]:
    pass_type_id = (os.getenv("APPLE_PASS_TYPE_ID") or "").strip()
    team_id = (os.getenv("APPLE_TEAM_ID") or "").strip()
    p12 = (os.getenv("APPLE_PASS_CERT_P12") or "").strip()
    wwdr = (os.getenv("APPLE_WWDR_CERT") or "").strip()
    if not (pass_type_id and team_id and p12 and wwdr):
        return None
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import pkcs12

        password = os.getenv("APPLE_PASS_CERT_PASSWORD") or ""
        key, cert, _ = pkcs12.load_key_and_certificates(
            base64.b64decode(p12), password.encode() if password else None
        )
        raw = base64.b64decode(wwdr)
        try:
            intermediate = x509.load_der_x509_certificate(raw)
        except ValueError:
            intermediate = x509.load_pem_x509_certificate(raw)
        return AppleCredentials(pass_type_id, team_id, cert, key, intermediate)
    except Exception:  # noqa: BLE001 — a bad credential disables the wallet, never the app
        logger.exception("Apple Wallet credentials could not be loaded; Apple passes are off.")
        return None


@lru_cache(maxsize=1)
def google() -> Optional[tuple[str, dict]]:
    """(issuer id, service account info), or None."""
    issuer = (os.getenv("GOOGLE_WALLET_ISSUER_ID") or "").strip()
    raw = (os.getenv("GOOGLE_WALLET_SERVICE_ACCOUNT_JSON") or "").strip()
    if not (issuer and raw):
        return None
    try:
        info = json.loads(raw)
        if not info.get("client_email") or not info.get("private_key"):
            raise ValueError("service account JSON is missing client_email or private_key")
        return issuer, info
    except Exception:  # noqa: BLE001
        logger.exception("Google Wallet credentials could not be loaded; Google passes are off.")
        return None


def availability() -> dict:
    return {"apple": apple() is not None, "google": google() is not None}


def reset() -> None:
    """Forget cached credentials (tests, or after settings change)."""
    apple.cache_clear()
    google.cache_clear()


def public_base_url() -> str:
    """Where Apple and Google reach us (pass web service, logo image)."""
    return (os.getenv("BACKEND_URL") or "").rstrip("/")
