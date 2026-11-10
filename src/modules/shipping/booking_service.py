"""Booking the courier once the buyer has paid (Convelio plan D-5, D-6).

* **Only a paid order is booked** — queued from the payment webhook (and dev confirm), never
  from checkout, so unpaid work never ships.
* **Never two couriers for one artwork.** Convelio has no idempotency key and no way to read
  an order back, so the shipment is marked `booking` and committed *before* the call:
  - Convelio answers → `booked`, with its order id, reference and tracking link.
  - Convelio refuses → `failed`; ops can retry from the admin console.
  - No answer (timeout) → `unknown`, never retried automatically. Convelio's
    `order_created` webhook (it carries our quote id) settles it, or ops checks.
* The Convelio bill is booked to the ledger as a shipping cost when the booking succeeds,
  like a hand-entered courier cost (Studio 3 is invoiced by Convelio — D-1).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from src.modules.shipping import convelio_client, shipping_config
from src.shared.config.database import SessionLocal
from src.shared.models.order import Order
from src.shared.models.shipment import SHIPMENT_LABEL_CREATED, Shipment
from src.shared.models.shipping import (
    BOOKING_BOOKED,
    BOOKING_BOOKING,
    BOOKING_FAILED,
    BOOKING_UNKNOWN,
    QUOTE_USED,
    ShippingQuote,
)
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def needs_booking(order: Order) -> bool:
    return bool(order.shipping_quote_id) and shipping_config.enabled()


def queue_after_payment(db, order: Order) -> None:
    """Call inside the payment transaction: books the courier once it commits."""
    if not needs_booking(order):
        return
    from src.jobs.dispatch import enqueue_after_commit

    enqueue_after_commit(db, "src.modules.shipping.booking_service.book_order", str(order.id))


def book_order(order_id: str, *, retry: bool = False) -> dict:
    db = SessionLocal()
    try:
        order = db.execute(select(Order).where(Order.id == uuid.UUID(str(order_id))).with_for_update()).scalar_one_or_none()
        if order is None or not order.shipping_quote_id:
            return {"state": "skipped"}
        if order.status not in ("paid",):
            return {"state": "skipped", "reason": f"order is {order.status}"}
        quote = db.get(ShippingQuote, order.shipping_quote_id)
        shipment = db.execute(select(Shipment).where(Shipment.order_id == order.id)).scalar_one_or_none()
        if shipment is not None and shipment.booking_state in (BOOKING_BOOKED, BOOKING_BOOKING, BOOKING_UNKNOWN) \
                and not (retry and shipment.booking_state == BOOKING_FAILED):
            return {"state": shipment.booking_state}
        if shipment is not None and shipment.booking_state == BOOKING_FAILED and not retry:
            return {"state": BOOKING_FAILED}
        if shipment is None:
            shipment = Shipment(id=uuid.uuid4(), order_id=order.id, provider="convelio",
                                status=SHIPMENT_LABEL_CREATED)
            db.add(shipment)
        shipment.booking_state = BOOKING_BOOKING
        shipment.booking_error = None
        db.commit()  # D-6: recorded before Convelio is called
        provider_quote_id, order_ref = quote.provider_quote_id, str(order.id)
        shipment_id = shipment.id
    finally:
        db.close()

    try:
        result = convelio_client.create_order({"quote_id": provider_quote_id,
                                               "customer_reference_number": order_ref})
    except convelio_client.ConvelioTimeout as exc:
        _finish(shipment_id, BOOKING_UNKNOWN, error=str(exc.message))
        logger.error("Convelio booking for order %s timed out; waiting for order_created.", order_ref)
        return {"state": BOOKING_UNKNOWN}
    except AppError as exc:
        _finish(shipment_id, BOOKING_FAILED, error=exc.message)
        logger.error("Convelio booking for order %s failed: %s", order_ref, exc.message)
        return {"state": BOOKING_FAILED, "reason": exc.message}
    _finish(shipment_id, BOOKING_BOOKED, result=result)
    return {"state": BOOKING_BOOKED, "orderId": result.get("id")}


def _finish(shipment_id, state: str, *, result: Optional[dict] = None, error: Optional[str] = None) -> None:
    db = SessionLocal()
    try:
        shipment = db.execute(select(Shipment).where(Shipment.id == shipment_id).with_for_update()).scalar_one()
        if shipment.booking_state == BOOKING_BOOKED and state != BOOKING_BOOKED:
            return  # order_created got here first
        shipment.booking_state = state
        shipment.booking_error = (error or None) and error[:2000]
        if result:
            adopt(db, shipment, result)
        db.commit()
    finally:
        db.close()


def adopt(db, shipment: Shipment, result: dict) -> None:
    """Record a Convelio order on the shipment and book its cost (once). Caller commits."""
    from src.modules.payments import money

    shipment.provider = "convelio"
    shipment.provider_order_id = result.get("id") or shipment.provider_order_id
    shipment.provider_order_ref = result.get("order_reference_number") or shipment.provider_order_ref
    shipment.tracking_link = result.get("tracking_link") or shipment.tracking_link
    shipment.dashboard_link = result.get("dashboard_order_link") or shipment.dashboard_link
    shipment.booking_state = BOOKING_BOOKED
    shipment.shipment_date = shipment.shipment_date or _now()
    order = db.get(Order, shipment.order_id)
    quote = db.get(ShippingQuote, order.shipping_quote_id) if order and order.shipping_quote_id else None
    if quote is not None:
        quote.status = QUOTE_USED
        if shipment.actual_shipping_cost_cents is None:
            shipment.actual_shipping_cost_cents = quote.total_cents
            if quote.total_cents > 0:
                money.book_shipping_cost(db, order, quote.total_cents, commit=False)
