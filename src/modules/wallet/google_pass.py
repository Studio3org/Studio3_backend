"""Google Wallet: the "Add to Google Wallet" link, and keeping saved passes current.

The link is https://pay.google.com/gp/v/save/<JWT>, the JWT signed (RS256) with the Wallet
service account and carrying the event's EventTicketClass and one EventTicketObject per
ticket. Issuing needs no API call — Google creates the class and objects when the buyer saves.

A saved pass is Google's copy, so a later change (refunded, voided, event moved) is pushed to
it through the Wallet REST API. Object ids are derived from our ticket ids, so we always know
which object to change.
"""
from __future__ import annotations

import time

from src.modules.wallet.wallet_config import public_base_url
from src.shared.templates.ticket_email import local_time_label

SAVE_URL = "https://pay.google.com/gp/v/save/"
API = "https://walletobjects.googleapis.com/walletobjects/v1"
SCOPE = "https://www.googleapis.com/auth/wallet_object.issuer"

STATE_ACTIVE = "ACTIVE"
STATE_INACTIVE = "INACTIVE"


def _text(value: str) -> dict:
    return {"defaultValue": {"language": "en-US", "value": value}}


def class_id(issuer: str, event_id) -> str:
    return f"{issuer}.event-{event_id}"


def object_id(issuer: str, ticket_id) -> str:
    return f"{issuer}.ticket-{ticket_id}"


def event_class(issuer: str, event) -> dict:
    data = {
        "id": class_id(issuer, event.id),
        "issuerName": "Studio 3",
        "reviewStatus": "UNDER_REVIEW",
        "eventName": _text(event.title),
        "eventId": str(event.id),
        "dateTime": {"start": event.starts_at.isoformat(), "end": event.ends_at.isoformat()},
        "hexBackgroundColor": "#111111",
    }
    if event.venue_name or event.address:
        data["venue"] = {
            "name": _text(event.venue_name or event.address),
            "address": _text(event.address or event.venue_name),
        }
    if event.cover_media_url and event.cover_media_url.startswith("https://"):
        data["heroImage"] = {"sourceUri": {"uri": event.cover_media_url}}
    base = public_base_url()
    if base.startswith("https://"):
        data["logo"] = {"sourceUri": {"uri": f"{base}/api/wallet/assets/logo.png"}}
    return data


def ticket_object(issuer: str, *, event, ticket, type_name: str, code: str, position: int,
                  count: int, holder_name: str | None, active: bool = True) -> dict:
    data = {
        "id": object_id(issuer, ticket.id),
        "classId": class_id(issuer, event.id),
        "state": STATE_ACTIVE if active else STATE_INACTIVE,
        "barcode": {"type": "QR_CODE", "value": code, "alternateText": ticket.number},
        "ticketNumber": ticket.number,
        "ticketType": _text(type_name),
        "textModulesData": [
            {"id": "when", "header": "When", "body": local_time_label(event)},
            {"id": "position", "header": "Ticket", "body": f"{position} of {count}"},
        ],
    }
    if holder_name:
        data["ticketHolderName"] = holder_name
    if event.latitude is not None and event.longitude is not None:
        data["locations"] = [{"latitude": event.latitude, "longitude": event.longitude}]
    return data


def save_url(issuer: str, service_account: dict, event_class_data: dict, objects: list[dict],
             origins: list[str]) -> str:
    """Sign the save link. `origins` are the web origins allowed to show the button."""
    from google.auth import crypt, jwt

    signer = crypt.RSASigner.from_service_account_info(service_account)
    claims = {
        "iss": service_account["client_email"],
        "aud": "google",
        "typ": "savetowallet",
        "iat": int(time.time()),
        "origins": origins,
        "payload": {"eventTicketClasses": [event_class_data], "eventTicketObjects": objects},
    }
    token = jwt.encode(signer, claims)
    return SAVE_URL + (token.decode() if isinstance(token, bytes) else token)


def _session(service_account: dict):
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account as sa

    creds = sa.Credentials.from_service_account_info(service_account, scopes=[SCOPE])
    return AuthorizedSession(creds)


def patch_object(issuer: str, service_account: dict, ticket_id, body: dict) -> bool:
    """Update a saved pass. A pass nobody saved is a 404, which is fine: nothing to update."""
    response = _session(service_account).patch(
        f"{API}/eventTicketObject/{object_id(issuer, ticket_id)}", json=body, timeout=15
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


def patch_class(issuer: str, service_account: dict, event_class_data: dict) -> bool:
    response = _session(service_account).patch(
        f"{API}/eventTicketClass/{event_class_data['id']}", json=event_class_data, timeout=15
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True
