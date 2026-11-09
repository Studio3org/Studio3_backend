"""Manual dispute resolution: refund the collector, or release the artist's payment.

Shared by the admin UI and any future JSON admin API so the two can't drift on what
"resolve" means. Every resolution records who did it, why, and when (FR-7.5).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.models.dispute import (
    DISPUTE_RESOLVED_RELEASE,
    Dispute,
)
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.orders import orders_dao
from src.modules.payments import payouts_service, refunds_service
from src.shared.models.refund import ROLE_ADMIN
from src.modules.notifications import notifications_dao

logger = get_logger(__name__)

# Kept for callers that list what an admin can refund; the rule itself is refunds_service's.
REFUNDABLE_ORDER_STATUSES = refunds_service.REFUNDABLE_ORDER_STATUSES


def _load(db, order_id: uuid.UUID):
    order = orders_dao.get_order_for_update(db, order_id)
    if not order:
        raise AppError("Order not found.", 404)
    dispute = db.execute(select(Dispute).where(Dispute.order_id == order_id)).scalar_one_or_none()
    return order, dispute


def refund_order(order_id: uuid.UUID, admin_id: uuid.UUID, reason: str) -> dict:
    """Refund the collector in full and reverse the order's ledger entries.

    The money movement itself lives in refunds_service, shared with the artist cancelling a
    paid order, so the two can't disagree on how a refund works. It holds the artist's payout
    before calling Stripe, which closes the race where a refund and a "confirm received"
    payout could both go out (PAY-4).

    Full refunds only — partial refunds would need the ledger reversal apportioned across
    commission, shipping and tax, which is a product decision that hasn't been made.
    """
    return refunds_service.refund_paid_order(
        order_id, actor_id=admin_id, actor_role=ROLE_ADMIN, reason=reason
    )


def release_order(order_id: uuid.UUID, admin_id: uuid.UUID, reason: str) -> dict:
    """Resolve in the artist's favour: complete the order and pay them through the normal
    payout path (FR-7.4) — no separate money-moving code to keep in sync."""
    db = SessionLocal()
    try:
        order, dispute = _load(db, order_id)
        if order.status not in ("disputed", "awaiting_confirmation"):
            raise AppError(f"Can't release an order in status '{order.status}'.", 409)

        order.received = True
        order.received_at = order.received_at or datetime.now(timezone.utc)
        orders_dao.transition_order(
            db, order, "completed", allowed_from={"disputed", "awaiting_confirmation"}, commit=False
        )
        if dispute:
            dispute.status = DISPUTE_RESOLVED_RELEASE
            dispute.resolved_by_admin_id = admin_id
            dispute.resolution_note = reason
            dispute.resolved_at = datetime.now(timezone.utc)
        db.commit()
        _notify(db, order, refunded=False)
    finally:
        db.close()

    result = payouts_service.release_payout_for_order(order_id)
    logger.info("Order %s released by admin %s: %s", order_id, admin_id, reason)
    return {"status": "released", "payout": result}


def _notify(db, order, refunded: bool) -> None:
    try:
        if refunded:
            notifications_dao.create_and_push(
                db, user_id=order.buyer_id, type="order", actor_id=None,
                target_type="order", target_id=order.id, payload={},
                title="Refund issued",
                body="We've refunded your order in full. It may take a few days to appear.",
            )
        else:
            notifications_dao.create_and_push(
                db, user_id=order.seller_id, type="order", actor_id=None,
                target_type="order", target_id=order.id, payload={},
                title="Payment released",
                body="The issue on your sale was resolved and your payment is on its way.",
            )
    except Exception as e:
        logger.warning("Dispute notification failed for order %s: %s", order.id, e)
