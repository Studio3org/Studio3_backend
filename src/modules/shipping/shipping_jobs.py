"""Daily shipping jobs (Phase 8).

* `orders.auto_complete_delivered` (D-9): a delivered order the buyer never confirmed
  completes ORDER_AUTO_COMPLETE_DAYS after delivery — unless a dispute is open — and the
  artist's payout is released, the same as tapping "Confirm received". Convelio's proof of
  delivery is what makes this safe; a hand-entered delivery counts too.
* `shipping.flag_stale_shipments`: booked shipments with no Convelio update for
  STALE_SHIPMENT_DAYS, and bookings whose outcome is still unknown after an hour, are logged
  for ops (the admin console lists them under Needs attention).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.modules.shipping import shipping_config
from src.shared.config.database import SessionLocal
from src.shared.models.dispute import DISPUTE_OPEN, Dispute
from src.shared.models.order import Order
from src.shared.models.shipment import Shipment
from src.shared.models.shipping import BOOKING_BOOKED, BOOKING_UNKNOWN
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)


def _now():
    return datetime.now(timezone.utc)


def auto_complete_delivered(now=None) -> int:
    from src.modules.orders import orders_dao
    from src.modules.payments import payouts_service

    now = now or _now()
    cutoff = now - timedelta(days=shipping_config.auto_complete_days())
    db = SessionLocal()
    done = []
    try:
        due = db.execute(
            select(Order.id).join(Shipment, Shipment.order_id == Order.id).where(
                Order.status == "awaiting_confirmation",
                Shipment.delivered_at.is_not(None), Shipment.delivered_at <= cutoff,
            )
        ).scalars().all()
        for order_id in due:
            order = orders_dao.get_order_for_update(db, order_id)
            if order is None or order.status != "awaiting_confirmation":
                db.rollback()
                continue
            if db.execute(select(Dispute.id).where(Dispute.order_id == order.id,
                                                   Dispute.status == DISPUTE_OPEN)).first():
                db.rollback()
                continue
            order.received = True
            order.received_at = now
            orders_dao.transition_order(db, order, "completed", commit=False)
            db.commit()
            done.append(order_id)
    finally:
        db.close()
    for order_id in done:
        try:
            payouts_service.release_payout_for_order(order_id)
        except Exception:  # noqa: BLE001 — the order stays completed; payout retryable by ops
            logger.exception("Payout after auto-complete failed for order %s", order_id)
    return len(done)


def flag_stale_shipments(now=None) -> dict:
    now = now or _now()
    db = SessionLocal()
    try:
        quiet = db.execute(
            select(Shipment.order_id).where(
                Shipment.provider == "convelio", Shipment.booking_state == BOOKING_BOOKED,
                Shipment.delivered_at.is_(None),
                (Shipment.last_event_at.is_(None) & (Shipment.created_at <= now - timedelta(days=shipping_config.STALE_SHIPMENT_DAYS)))
                | (Shipment.last_event_at <= now - timedelta(days=shipping_config.STALE_SHIPMENT_DAYS)),
            )
        ).scalars().all()
        unknown = db.execute(
            select(Shipment.order_id).where(Shipment.booking_state == BOOKING_UNKNOWN,
                                            Shipment.updated_at <= now - timedelta(hours=1))
        ).scalars().all()
        for order_id in quiet:
            logger.error("Convelio shipment for order %s has had no update for %s days.",
                         order_id, shipping_config.STALE_SHIPMENT_DAYS)
        for order_id in unknown:
            logger.error("Convelio booking for order %s is still unknown after an hour.", order_id)
        return {"quiet": len(quiet), "unknown": len(unknown)}
    finally:
        db.close()
