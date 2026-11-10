"""Wallet passes for event tickets (BACKEND_CHANGES B25; the client requires Google Wallet).

* `order_passes`: what GET …/orders/:id/wallet-pass returns — a Google save link, or an Apple
  .pkpass (one ticket) / .pkpasses (several).
* Signed download links, so the ticket email can offer "Add to Apple Wallet" without a login.
* Apple's pass web service: devices register for a pass, ask what changed, fetch the latest.
* `tickets_changed`: after a ticket is voided, refunded or its event moves, push the change to
  Google's copy and to every Apple device holding it. Queued (src.jobs.dispatch) so a slow
  Google or Apple call never holds up the refund or reschedule that caused it.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.modules.wallet import apple_pass, google_pass, wallet_config
from src.shared.models.event import Event
from src.shared.models.ticketing import (
    LIVE_TICKET_STATUSES,
    ORDER_CANCELLED,
    ORDER_PAID,
    ORDER_REFUNDED,
    RSVP_TICKET_NAME,
    Ticket,
    TicketOrder,
    TicketType,
    WalletAppleRegistration,
)
from src.shared.models.user import User
from src.shared.ticketing import ticket_codes
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

DOWNLOAD_LINK_TTL = 60 * 60 * 24 * 30  # the ticket email's Apple link works for 30 days
APNS_HOST = "https://api.push.apple.com"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def availability() -> dict:
    """`walletPasses` on orders and tickets, and `walletPassAvailable` (B25's single flag)."""
    available = wallet_config.availability()
    return {"walletPasses": available, "walletPassAvailable": any(available.values())}


# --- the passes for an order ---------------------------------------------------------------

def _order_tickets(db: Session, order: TicketOrder, ticket_id: Optional[uuid.UUID]) -> list:
    stmt = (
        select(Ticket, TicketType.name)
        .outerjoin(TicketType, TicketType.id == Ticket.ticket_type_id)
        .where(Ticket.order_id == order.id)
        .order_by(Ticket.created_at, Ticket.number)
    )
    rows = db.execute(stmt).all()
    live = [(t, n or RSVP_TICKET_NAME) for t, n in rows if t.status in LIVE_TICKET_STATUSES]
    if ticket_id is not None:
        live = [(t, n) for t, n in live if t.id == ticket_id]
    return live


def _checked_order(db: Session, order: Optional[TicketOrder]) -> TicketOrder:
    if order is None:
        raise AppError("Order not found.", 404)
    if order.status in (ORDER_CANCELLED, ORDER_REFUNDED):
        raise AppError("These tickets are no longer valid.", 410)
    if order.status != ORDER_PAID:
        raise AppError("The tickets haven't been issued yet.", 409)
    return order


def order_passes(db: Session, order: TicketOrder, *, platform: str,
                 ticket_id: Optional[uuid.UUID] = None) -> dict:
    """{"saveUrl"} for Google, or {"body", "mimetype", "filename"} for Apple."""
    order = _checked_order(db, order)
    tickets = _order_tickets(db, order, ticket_id)
    if not tickets:
        raise AppError("These tickets are no longer valid.", 410)
    event = db.get(Event, order.event_id)
    holder = db.get(User, order.buyer_id)
    holder_name = (holder.name or holder.username) if holder else None
    count = len(_order_tickets(db, order, None))
    positions = {t.id: i + 1 for i, (t, _) in enumerate(_order_tickets(db, order, None))}

    if platform == "google":
        creds = wallet_config.google()
        if creds is None:
            raise AppError("Google Wallet isn't set up yet.", 503)
        issuer, service_account = creds
        objects = [
            google_pass.ticket_object(
                issuer, event=event, ticket=t, type_name=name,
                code=ticket_codes.make_code(t.id, t.event_id),
                position=positions[t.id], count=count, holder_name=holder_name,
            )
            for t, name in tickets
        ]
        origins = [o for o in (os.getenv("FRONTEND_URL"), wallet_config.public_base_url()) if o]
        return {"saveUrl": google_pass.save_url(
            issuer, service_account, google_pass.event_class(issuer, event), objects, origins)}

    creds = wallet_config.apple()
    if creds is None:
        raise AppError("Apple Wallet isn't set up yet.", 503)
    built = [
        (t, apple_pass.build_pkpass(creds, apple_pass.pass_json(
            creds, event=event, ticket=t, type_name=name,
            code=ticket_codes.make_code(t.id, t.event_id),
            position=positions[t.id], count=count, holder_name=holder_name, voided=False,
        )))
        for t, name in tickets
    ]
    if len(built) == 1:
        ticket, body = built[0]
        return {"body": body, "mimetype": apple_pass.PKPASS_MIME,
                "filename": f"ticket-{ticket.number}.pkpass"}
    return {
        "body": apple_pass.build_bundle([(f"ticket-{t.number}.pkpass", b) for t, b in built]),
        "mimetype": apple_pass.PKPASSES_MIME,
        "filename": f"tickets-{str(order.id)[:8]}.pkpasses",
    }


# --- signed links for the email ------------------------------------------------------------

def _sign(text: str) -> str:
    secret = (os.getenv("SECRET_KEY") or "dev-secret-key-change-me").encode()
    return hmac.new(secret, text.encode(), hashlib.sha256).hexdigest()[:32]


def apple_download_url(order_id) -> Optional[str]:
    base = wallet_config.public_base_url()
    if not base or wallet_config.apple() is None:
        return None
    expires = int(time.time()) + DOWNLOAD_LINK_TTL
    return f"{base}/api/wallet/apple/orders/{order_id}.pkpass?exp={expires}&sig={_sign(f'{order_id}:{expires}')}"


def check_download_signature(order_id: str, exp: str, sig: str) -> bool:
    try:
        expires = int(exp)
    except (TypeError, ValueError):
        return False
    return expires >= time.time() and hmac.compare_digest(_sign(f"{order_id}:{expires}"), sig or "")


def email_links(db: Session, order: TicketOrder) -> dict:
    """{"google": url|None, "apple": url|None} for the confirmation email. Never raises."""
    links = {"google": None, "apple": apple_download_url(order.id)}
    if wallet_config.google() is not None:
        try:
            links["google"] = order_passes(db, order, platform="google")["saveUrl"]
        except Exception:  # noqa: BLE001 — an email without a wallet link is still an email
            logger.exception("Could not make the Google Wallet link for order %s", order.id)
    return links


# --- Apple's pass web service ----------------------------------------------------------------

def _authorised_ticket(db: Session, pass_type_id: str, serial: str, auth_header: str) -> Ticket:
    creds = wallet_config.apple()
    if creds is None or pass_type_id != creds.pass_type_id:
        raise AppError("Unknown pass type.", 404)
    token = (auth_header or "").removeprefix("ApplePass ").strip()
    if not hmac.compare_digest(token, apple_pass.authentication_token(serial)):
        raise AppError("Not authorised.", 401)
    try:
        ticket = db.get(Ticket, uuid.UUID(serial))
    except ValueError:
        ticket = None
    if ticket is None:
        raise AppError("Pass not found.", 404)
    return ticket


def register_device(db: Session, device_id: str, pass_type_id: str, serial: str,
                    push_token: str, auth_header: str) -> int:
    """201 when newly registered, 200 when already known (Apple's contract)."""
    ticket = _authorised_ticket(db, pass_type_id, serial, auth_header)
    existing = db.execute(select(WalletAppleRegistration).where(
        WalletAppleRegistration.device_library_id == device_id,
        WalletAppleRegistration.pass_type_id == pass_type_id,
        WalletAppleRegistration.ticket_id == ticket.id,
    )).scalar_one_or_none()
    if existing is not None:
        existing.push_token = push_token or existing.push_token
        db.commit()
        return 200
    db.add(WalletAppleRegistration(
        id=uuid.uuid4(), device_library_id=device_id, push_token=push_token,
        pass_type_id=pass_type_id, ticket_id=ticket.id,
    ))
    db.commit()
    return 201


def unregister_device(db: Session, device_id: str, pass_type_id: str, serial: str,
                      auth_header: str) -> None:
    ticket = _authorised_ticket(db, pass_type_id, serial, auth_header)
    for row in db.execute(select(WalletAppleRegistration).where(
        WalletAppleRegistration.device_library_id == device_id,
        WalletAppleRegistration.pass_type_id == pass_type_id,
        WalletAppleRegistration.ticket_id == ticket.id,
    )).scalars():
        db.delete(row)
    db.commit()


def updated_serials(db: Session, device_id: str, pass_type_id: str,
                    since: Optional[str]) -> Optional[dict]:
    """The device's passes changed since `since` (our own tag), or None when nothing did."""
    stmt = (
        select(Ticket.id, Ticket.pass_updated_at)
        .join(WalletAppleRegistration, WalletAppleRegistration.ticket_id == Ticket.id)
        .where(WalletAppleRegistration.device_library_id == device_id,
               WalletAppleRegistration.pass_type_id == pass_type_id)
    )
    if since:
        try:
            stmt = stmt.where(Ticket.pass_updated_at > datetime.fromtimestamp(float(since), timezone.utc))
        except ValueError:
            pass
    rows = db.execute(stmt).all()
    if not rows:
        return None
    latest = max(updated for _, updated in rows)
    return {"serialNumbers": [str(t) for t, _ in rows], "lastUpdated": f"{latest.timestamp():.6f}"}


def latest_pass(db: Session, pass_type_id: str, serial: str, auth_header: str) -> tuple[bytes, datetime]:
    """The current .pkpass for one ticket — voided if it no longer admits anyone."""
    ticket = _authorised_ticket(db, pass_type_id, serial, auth_header)
    creds = wallet_config.apple()
    order = db.get(TicketOrder, ticket.order_id)
    event = db.get(Event, ticket.event_id)
    holder = db.get(User, ticket.holder_id)
    siblings = db.execute(
        select(Ticket.id).where(Ticket.order_id == order.id).order_by(Ticket.created_at, Ticket.number)
    ).scalars().all()
    type_name = (db.get(TicketType, ticket.ticket_type_id).name
                 if ticket.ticket_type_id else RSVP_TICKET_NAME)
    body = apple_pass.build_pkpass(creds, apple_pass.pass_json(
        creds, event=event, ticket=ticket, type_name=type_name,
        code=ticket_codes.make_code(ticket.id, ticket.event_id),
        position=siblings.index(ticket.id) + 1, count=len(siblings),
        holder_name=(holder.name or holder.username) if holder else None,
        voided=ticket.status not in LIVE_TICKET_STATUSES,
    ))
    return body, ticket.pass_updated_at


# --- passes follow their tickets -------------------------------------------------------------

def tickets_changed(db: Session, ticket_ids) -> None:
    """Call when a ticket's status or its event changed, before the caller's commit: marks the
    passes changed, and once that commit lands, pushes the change to Google and Apple."""
    ids = [uuid.UUID(str(t)) for t in ticket_ids]
    if not ids:
        return
    now = _now()
    for ticket in db.execute(select(Ticket).where(Ticket.id.in_(ids))).scalars():
        ticket.pass_updated_at = now
    from src.jobs.dispatch import enqueue_after_commit

    enqueue_after_commit(db, "src.modules.wallet.wallet_service.push_updates",
                         ",".join(str(i) for i in ids))


def event_changed(db: Session, event, ticket_ids) -> None:
    """The event moved (Phase 4 reschedule): every pass shows the new date. Apple devices are
    pushed and fetch a pass built from the event as it is now; Google's copy lives in the
    event's class, which is patched once the caller commits."""
    tickets_changed(db, ticket_ids)
    from src.jobs.dispatch import enqueue_after_commit

    enqueue_after_commit(db, "src.modules.wallet.wallet_service.push_event_class", str(event.id))


def push_event_class(event_id: str) -> None:
    from src.shared.config.database import SessionLocal
    from src.shared.models.event import Event

    google = wallet_config.google()
    if google is None:
        return
    issuer, service_account = google
    db = SessionLocal()
    try:
        event = db.get(Event, uuid.UUID(event_id))
        if event is None:
            return
        try:
            google_pass.patch_class(issuer, service_account, google_pass.event_class(issuer, event))
        except Exception:  # noqa: BLE001
            logger.exception("Google Wallet class update failed for event %s", event_id)
    finally:
        db.close()


def push_updates(ticket_ids_csv: str) -> None:
    """Background: bring Google's copies and Apple devices up to date. Safe to run twice."""
    from src.shared.config.database import SessionLocal

    ids = [uuid.UUID(t) for t in ticket_ids_csv.split(",") if t]
    db = SessionLocal()
    try:
        tickets = db.execute(select(Ticket).where(Ticket.id.in_(ids))).scalars().all()
        google = wallet_config.google()
        if google is not None:
            issuer, service_account = google
            for ticket in tickets:
                state = (google_pass.STATE_ACTIVE if ticket.status in LIVE_TICKET_STATUSES
                         else google_pass.STATE_INACTIVE)
                try:
                    google_pass.patch_object(issuer, service_account, ticket.id, {"state": state})
                except Exception:  # noqa: BLE001
                    logger.exception("Google Wallet update failed for ticket %s", ticket.id)
        tokens = db.execute(
            select(WalletAppleRegistration.push_token).where(WalletAppleRegistration.ticket_id.in_(ids))
        ).scalars().all()
        if tokens and wallet_config.apple() is not None:
            _push_apple(sorted(set(tokens)))
    finally:
        db.close()


def _push_apple(tokens: list[str]) -> None:
    """Tell Apple devices a pass changed: an empty push on the pass type's topic, sent with the
    Pass Type ID certificate (Apple's rule for Wallet). The device then fetches the new pass."""
    import httpx
    from cryptography.hazmat.primitives import serialization

    creds = wallet_config.apple()
    with tempfile.NamedTemporaryFile(suffix=".pem") as cert_file, \
            tempfile.NamedTemporaryFile(suffix=".pem") as key_file:
        cert_file.write(creds.certificate.public_bytes(serialization.Encoding.PEM))
        key_file.write(creds.private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        cert_file.flush()
        key_file.flush()
        with httpx.Client(http2=True, cert=(cert_file.name, key_file.name), timeout=10) as client:
            for token in tokens:
                try:
                    response = client.post(f"{APNS_HOST}/3/device/{token}", content=b"{}",
                                           headers={"apns-topic": creds.pass_type_id})
                    if response.status_code != 200:
                        logger.warning("APNs pass push %s: %s", response.status_code, response.text)
                except Exception:  # noqa: BLE001
                    logger.exception("APNs pass push failed")
