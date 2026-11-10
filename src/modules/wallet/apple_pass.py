"""Apple Wallet: build and sign a ticket's .pkpass, or a .pkpasses bundle of several.

A .pkpass is a zip of pass.json, the images, manifest.json (SHA-1 of every other file) and
`signature` — a detached PKCS#7 signature of the manifest made with the Pass Type ID
certificate, carrying Apple's WWDR intermediate certificate. Built in memory with
`cryptography`; nothing touches disk.

The barcode is the ticket's signed code (ticket_codes.py), the same value the app shows and the
door checks, so a pass admits exactly the ticket it was made from.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7

from src.modules.wallet.wallet_config import AppleCredentials, public_base_url
from src.shared.templates.ticket_email import local_time_label

ASSETS = Path(__file__).resolve().parent / "assets"
IMAGE_FILES = ("icon.png", "icon@2x.png", "icon@3x.png", "logo.png")

PKPASS_MIME = "application/vnd.apple.pkpass"
PKPASSES_MIME = "application/vnd.apple.pkpasses"


def authentication_token(serial: str) -> str:
    """The token Apple sends back with every pass web service call for this pass.

    Derived, not stored: HMAC of the serial with SECRET_KEY. Only a pass we issued carries the
    right one, and nothing needs a table to check it."""
    secret = (os.getenv("SECRET_KEY") or "dev-secret-key-change-me").encode()
    return hmac.new(secret, f"wallet-pass:{serial}".encode(), hashlib.sha256).hexdigest()[:40]


def web_service_url() -> str | None:
    """Apple only calls an https web service; without one, passes simply don't update."""
    base = public_base_url()
    return f"{base}/api/wallet/apple" if base.startswith("https://") else None


def pass_json(creds: AppleCredentials, *, event, ticket, type_name: str, code: str,
              position: int, count: int, holder_name: str | None, voided: bool) -> dict:
    """pass.json for one ticket (Apple's eventTicket style)."""
    where = event.venue_name or event.address or ""
    data = {
        "formatVersion": 1,
        "passTypeIdentifier": creds.pass_type_id,
        "teamIdentifier": creds.team_id,
        "serialNumber": str(ticket.id),
        "organizationName": "Studio 3",
        "description": f"Ticket for {event.title}",
        "foregroundColor": "rgb(255, 255, 255)",
        "labelColor": "rgb(170, 170, 170)",
        "backgroundColor": "rgb(17, 17, 17)",
        "relevantDate": event.starts_at.isoformat(),
        "expirationDate": event.ends_at.isoformat(),
        "voided": voided,
        "barcodes": [{
            "format": "PKBarcodeFormatQR",
            "message": code,
            "messageEncoding": "iso-8859-1",
            "altText": ticket.number,
        }],
        "eventTicket": {
            "primaryFields": [{"key": "event", "label": "EVENT", "value": event.title}],
            "secondaryFields": [
                {"key": "date", "label": "WHEN", "value": local_time_label(event)},
                *([{"key": "venue", "label": "WHERE", "value": where}] if where else []),
            ],
            "auxiliaryFields": [
                {"key": "type", "label": "TICKET", "value": type_name},
                {"key": "position", "label": "", "value": f"{position} of {count}"},
                *([{"key": "age", "label": "AGE", "value": f"{event.age_limit}+"}]
                  if event.age_limit in ("18", "21") else []),
            ],
            "backFields": [
                {"key": "number", "label": "Ticket number", "value": ticket.number},
                *([{"key": "holder", "label": "Name", "value": holder_name}] if holder_name else []),
                *([{"key": "address", "label": "Address", "value": event.address}]
                  if event.address else []),
                {"key": "entry", "label": "Entry",
                 "value": "Each ticket can be checked in once. Bring your ID for age-limited events."},
            ],
        },
    }
    if event.latitude is not None and event.longitude is not None:
        data["locations"] = [{"latitude": event.latitude, "longitude": event.longitude}]
    service = web_service_url()
    if service:
        data["webServiceURL"] = service
        data["authenticationToken"] = authentication_token(str(ticket.id))
    return data


def _sign(creds: AppleCredentials, manifest: bytes) -> bytes:
    return (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(manifest)
        .add_signer(creds.certificate, creds.private_key, hashes.SHA256())
        .add_certificate(creds.wwdr)
        .sign(serialization.Encoding.DER,
              [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary])
    )


def build_pkpass(creds: AppleCredentials, pass_data: dict) -> bytes:
    files = {"pass.json": json.dumps(pass_data, ensure_ascii=False).encode("utf-8")}
    for name in IMAGE_FILES:
        files[name] = (ASSETS / name).read_bytes()
    manifest = json.dumps(
        {name: hashlib.sha1(body).hexdigest() for name, body in files.items()}, sort_keys=True
    ).encode()
    files["manifest.json"] = manifest
    files["signature"] = _sign(creds, manifest)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return out.getvalue()


def build_bundle(passes: list[tuple[str, bytes]]) -> bytes:
    """Several passes in one download (.pkpasses, iOS 15+): one per ticket in an order."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        for name, body in passes:
            archive.writestr(name, body)
    return out.getvalue()
