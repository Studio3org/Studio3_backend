"""Convelio's webhooks: `POST /api/webhooks/convelio` (Convelio plan D-7, D-8).

**Store first.** Convelio never redelivers after an error response (only after a network
failure), so the route verifies the signature, stores the event and answers 200 before doing
anything that could fail. Processing runs after, in the background, and a failure is kept on
the stored row for ops — never surfaced to Convelio.

**Signature:** `X-Convelio-signature` is the lowercase hex HMAC-SHA256 of the raw body, keyed
with our API key (Convelio's answer of 9 Nov). There's no event id, so duplicates are caught
by a hash of the body.

**Statuses (D-8)** map onto the existing order and shipment states — no new order states:

| Convelio | Shipment | Order |
|---|---|---|
| shipment_created | label_created | paid → shipped |
| picked_up | picked_up | |
| packing_in_progress … out_for_delivery | in_transit | |
| shipment_completed | delivered | shipped → awaiting_confirmation |
| on_hold | (unchanged) | buyer told; flagged for ops |
| canceled | booking canceled | flagged for ops (refund through the admin console) |
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.modules.shipping import shipping_config
from src.shared.config.database import SessionLocal
from src.shared.models.order import Order
from src.shared.models.shipment import (
    SHIPMENT_DELIVERED,
    SHIPMENT_IN_TRANSIT,
    SHIPMENT_LABEL_CREATED,
    SHIPMENT_PICKED_UP,
    Shipment,
)
from src.shared.models.shipping import (
    BOOKING_BOOKED,
    BOOKING_CANCELED,
    QUOTE_READY,
    ProviderWebhookEvent,
    ShipmentDocument,
    ShippingQuote,
)
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)
convelio_webhook_bp = Blueprint("convelio_webhook", __name__)

STATUS_MAP = {
    "shipment_created": SHIPMENT_LABEL_CREATED,
    "picked_up": SHIPMENT_PICKED_UP,
    "packing_in_progress": SHIPMENT_IN_TRANSIT,
    "export_in_progress": SHIPMENT_IN_TRANSIT,
    "freight_in_transit": SHIPMENT_IN_TRANSIT,
    "import_in_progress": SHIPMENT_IN_TRANSIT,
    "out_for_delivery": SHIPMENT_IN_TRANSIT,
    "shipment_completed": SHIPMENT_DELIVERED,
}
# The buyer hears about these.
TOLD = {
    "picked_up": ("Picked up", "The courier has collected your artwork."),
    "out_for_delivery": ("Out for delivery", "Your artwork is out for delivery today."),
    "on_hold": ("Shipment on hold", "Your shipment is on hold. We're looking into it and will be in touch."),
    "canceled": ("Shipment cancelled", "Your shipment was cancelled. We'll be in touch about what happens next."),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def valid_signature(raw: bytes, signature: str) -> bool:
    key = shipping_config.api_key()
    if not key or not signature:
        return False
    expected = hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


@convelio_webhook_bp.post("/convelio")
def receive():
    raw = request.get_data(cache=False)
    if not valid_signature(raw, request.headers.get("X-Convelio-signature", "")):
        return {"success": False, "message": "Invalid signature."}, 401
    try:
        event = json.loads(raw or b"{}")
    except ValueError:
        return {"success": False, "message": "Invalid JSON."}, 400
    digest = hashlib.sha256(raw).hexdigest()
    db = SessionLocal()
    try:
        row = ProviderWebhookEvent(id=uuid.uuid4(), provider="convelio", body_hash=digest,
                                   event_type=str(event.get("event") or "unknown")[:64], payload=event)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return {"success": True, "duplicate": True}, 200
        from src.jobs.dispatch import enqueue

        enqueue("src.modules.shipping.convelio_webhook.process_event", str(row.id))
        return {"success": True}, 200
    finally:
        db.close()


# --- processing (background) -------------------------------------------------------------------

def process_event(event_row_id: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(ProviderWebhookEvent, uuid.UUID(event_row_id))
        if row is None or row.processed_at is not None:
            return
        try:
            _handle(db, row.event_type, row.payload.get("payload") or {})
            row.processed_at = _now()
            row.error = None
            db.commit()
        except Exception as exc:  # noqa: BLE001 — kept on the row for ops; Convelio never sees it
            db.rollback()
            row = db.get(ProviderWebhookEvent, uuid.UUID(event_row_id))
            row.error = str(exc)[:2000]
            db.commit()
            logger.exception("Convelio event %s (%s) failed", event_row_id, row.event_type)
    finally:
        db.close()


def _shipment_for(db, order_id) -> Shipment | None:
    return db.execute(select(Shipment).where(Shipment.provider_order_id == order_id).with_for_update()).scalar_one_or_none()


def _handle(db, kind: str, payload: dict) -> None:
    if kind == "custom_quote_ready":
        _quote_ready(db, payload.get("quote_id"))
    elif kind == "order_created":
        _order_created(db, payload.get("quote_id"), payload.get("order_id"))
    elif kind == "shipment_status_changed":
        _status(db, payload.get("order_id"), payload.get("status"))
    elif kind == "document_ready":
        shipment = _shipment_for(db, payload.get("order_id"))
        if shipment is not None:
            exists = db.execute(select(ShipmentDocument.id).where(
                ShipmentDocument.shipment_id == shipment.id,
                ShipmentDocument.document_type == payload.get("document_type"))).first()
            if not exists:
                db.add(ShipmentDocument(id=uuid.uuid4(), shipment_id=shipment.id,
                                        document_type=str(payload.get("document_type"))[:64],
                                        dashboard_link=payload.get("dashboard_order_link")))
    elif kind == "quote_paid":
        pass  # Convelio's own share-link payment: we never use it (D-1)
    else:
        logger.info("Ignoring Convelio event %s", kind)


def _quote_ready(db, provider_quote_id: str) -> None:
    from src.modules.shipping import quote_service

    quote = quote_service.pending_for(db, provider_quote_id)
    if quote is None:
        logger.warning("custom_quote_ready for unknown quote %s", provider_quote_id)
        return
    quote_service.refresh(db, quote)
    if quote.status == QUOTE_READY:
        _notify(db, quote.buyer_id, "shipping_quote_ready", "piece", quote.piece_id,
                "Shipping quote ready", "Your shipping price is in. Finish checking out before it expires.",
                {"quoteId": str(quote.id), "expiresAt": quote.expires_at.isoformat() if quote.expires_at else None})


def _order_created(db, provider_quote_id: str, provider_order_id: str) -> None:
    """Settles a booking whose answer we never got (D-6)."""
    quote = db.execute(select(ShippingQuote).where(ShippingQuote.provider_quote_id == provider_quote_id)).scalar_one_or_none()
    if quote is None:
        return
    order = db.execute(select(Order).where(Order.shipping_quote_id == quote.id)).scalars().first()
    if order is None:
        return
    shipment = db.execute(select(Shipment).where(Shipment.order_id == order.id).with_for_update()).scalar_one_or_none()
    if shipment is None or shipment.booking_state == BOOKING_BOOKED:
        return
    from src.modules.shipping import booking_service

    booking_service.adopt(db, shipment, {"id": provider_order_id})


def _status(db, provider_order_id: str, status: str) -> None:
    from src.modules.orders import orders_dao

    shipment = _shipment_for(db, provider_order_id)
    if shipment is None:
        logger.warning("Status %s for unknown Convelio order %s", status, provider_order_id)
        return
    order = db.get(Order, shipment.order_id)
    shipment.provider_status = status
    shipment.last_event_at = _now()
    mapped = STATUS_MAP.get(status)
    if mapped:
        shipment.status = mapped
    if status == "shipment_created" and order.status == "paid":
        orders_dao.transition_order(db, order, "shipped", allowed_from={"paid"}, commit=False)
    if status in ("picked_up", "packing_in_progress", "freight_in_transit", "out_for_delivery") \
            and order.status == "paid":
        orders_dao.transition_order(db, order, "shipped", allowed_from={"paid"}, commit=False)
    if status == "shipment_completed":
        shipment.delivered_at = shipment.delivered_at or _now()
        if order.status == "paid":
            orders_dao.transition_order(db, order, "shipped", allowed_from={"paid"}, commit=False)
        if order.status == "shipped":
            orders_dao.transition_order(db, order, "awaiting_confirmation", allowed_from={"shipped"},
                                        commit=False)
        _notify(db, order.buyer_id, "order_delivered", "order", order.id, "Your artwork has arrived",
                "Confirm you received it in good condition to release payment to the artist.",
                {"side": "buyer"})
    if status == "canceled":
        shipment.booking_state = BOOKING_CANCELED
    if status in TOLD:
        title, body = TOLD[status]
        _notify(db, order.buyer_id, f"shipment_{status}", "order", order.id, title, body,
                {"side": "buyer", "status": status})
    if status in ("on_hold", "canceled"):
        logger.error("Convelio shipment for order %s is %s — needs ops.", order.id, status)


def _notify(db, user_id, type_, target_type, target_id, title, body, payload) -> None:
    from src.modules.notifications import notifications_dao

    try:
        notifications_dao.create_and_push(db, user_id=user_id, type=type_, target_type=target_type,
                                          target_id=target_id, title=title, body=body, payload=payload)
    except Exception:  # noqa: BLE001
        logger.exception("Shipping notification %s failed for %s", type_, user_id)
