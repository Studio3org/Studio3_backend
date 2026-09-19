"""Manual dispute resolution: refund the collector, or release the artist's payment.

Shared by the admin UI and any future JSON admin API so the two can't drift on what
"resolve" means. Every resolution records who did it, why, and when (FR-7.5).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.config.stripe_client import get_stripe, stripe_configured
from src.shared.models.dispute import (
    DISPUTE_RESOLVED_REFUND,
    DISPUTE_RESOLVED_RELEASE,
    Dispute,
)
from src.shared.models.payout import PAYOUT_BLOCKED, PAYOUT_RELEASED, Payout
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.orders import orders_dao
from src.modules.payments import money, payouts_service
from src.modules.notifications import notifications_dao

logger = get_logger(__name__)

# A refund can only happen while the platform still holds the money. Once a payout is
# released the funds are in the artist's Stripe account and this path can't claw them back.
REFUNDABLE_ORDER_STATUSES = ("paid", "shipped", "awaiting_confirmation", "disputed")


def _load(db, order_id: uuid.UUID):
    order = orders_dao.get_order_for_update(db, order_id)
    if not order:
        raise AppError("Order not found.", 404)
    dispute = db.execute(select(Dispute).where(Dispute.order_id == order_id)).scalar_one_or_none()
    return order, dispute


def refund_order(order_id: uuid.UUID, admin_id: uuid.UUID, reason: str) -> dict:
    """Refund the collector in full and reverse the order's ledger entries.

    Phase 1 is full refunds only — partial refunds would need the ledger reversal to be
    apportioned across commission, shipping and tax, which is a product decision that
    hasn't been made.
    """
    db = SessionLocal()
    try:
        order, dispute = _load(db, order_id)

        payout = db.execute(select(Payout).where(Payout.order_id == order_id)).scalar_one_or_none()
        if payout and payout.status == PAYOUT_RELEASED:
            raise AppError(
                "The artist has already been paid for this order; a refund here would leave "
                "the platform out of pocket. Recover the transfer in Stripe first.",
                409,
            )
        if order.status == "refunded":
            return {"status": "already_refunded"}
        if order.status not in REFUNDABLE_ORDER_STATUSES:
            raise AppError(f"Can't refund an order in status '{order.status}'.", 409)

        charge_id = order.stripe_charge_id
        total = order.total_cents
    finally:
        db.close()

    # Stripe call outside the transaction — same reasoning as payout release.
    stripe_fee = 0
    if stripe_configured() and charge_id:
        stripe = get_stripe()
        try:
            stripe.Refund.create(
                charge=charge_id,
                reason="requested_by_customer",
                metadata={"order_id": str(order_id), "admin_id": str(admin_id)},
                idempotency_key=f"refund:{order_id}",
            )
            # The original charge's fee, which Stripe keeps on a refund — the ledger
            # reversal has to account for it rather than netting to zero.
            charge = stripe.Charge.retrieve(charge_id, expand=["balance_transaction"])
            bt = charge.get("balance_transaction")
            stripe_fee = int(bt.get("fee", 0)) if isinstance(bt, dict) else 0
        except Exception as e:
            logger.exception("Refund failed for order %s: %s", order_id, e)
            raise AppError(f"Stripe refund failed: {e}", 502)

    db = SessionLocal()
    try:
        order, dispute = _load(db, order_id)
        if order.status != "refunded":
            orders_dao.transition_order(db, order, "refunded", commit=False)
            orders_dao.release_pieces(db, order, commit=False)
            money.book_refund_issued(db, order, stripe_fee_cents=stripe_fee, commit=False)
        if dispute:
            dispute.status = DISPUTE_RESOLVED_REFUND
            dispute.resolved_by_admin_id = admin_id
            dispute.resolution_note = reason
            dispute.resolved_at = datetime.now(timezone.utc)
        if payout_row := db.execute(
            select(Payout).where(Payout.order_id == order_id)
        ).scalar_one_or_none():
            # BLOCKED, not TRANSFER_FAILED: nothing was attempted and nothing should be
            # retried. TRANSFER_FAILED is in RELEASABLE_STATUSES, so labelling a deliberate
            # cancellation that way puts refunded money back in reach of a release.
            payout_row.status = PAYOUT_BLOCKED
            payout_row.failure_reason = "Order refunded; payout cancelled."
        db.commit()
        _notify(db, order, refunded=True)
        logger.info("Order %s refunded by admin %s: %s", order_id, admin_id, reason)
        return {"status": "refunded", "amountCents": total}
    finally:
        db.close()


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
