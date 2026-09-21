"""Releasing artwork that an abandoned checkout is still holding.

Creating an order reserves its piece immediately, before any money moves — without that,
two collectors could pay for the same one-of-a-kind work while both were at the card
screen. The reservation is released when payment fails, or the order is cancelled or
refunded.

Every one of those releases depends on Stripe telling us something happened. Nothing does
when a collector simply closes the payment sheet, force-quits, or loses signal: no webhook
fires, the order stays `pending_payment`, and the piece stays `reserved` **forever**. The
artist cannot relist it and no one else can buy it. Changing your mind at the card screen is
completely ordinary behaviour, so without this sweep the marketplace quietly loses work to
it.

The rule this applies is deliberately narrow: an order is only abandoned if Stripe agrees it
was never paid. We ask Stripe to cancel the PaymentIntent first and let it arbitrate — if it
refuses because the intent is `processing` or already `succeeded`, the money is in flight and
we leave the order alone for the webhook to finish. Releasing on our own clock instead would
mean cancelling an order that is about to be paid, and handing the piece to someone else
while the first collector's card is being charged.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.config.stripe_client import get_stripe, stripe_configured
from src.shared.models.order import Order
from src.shared.utils.logger import get_logger
from src.modules.orders import orders_dao

logger = get_logger(__name__)

# How long a collector gets between creating the order and paying for it.
#
# Long enough to find a card, be interrupted, and come back. Short enough that a piece is not
# off the market for an afternoon because somebody closed a sheet. The payment sheet itself is
# usually a sub-minute affair, so this is already generous.
ABANDONED_AFTER = timedelta(minutes=15)


def _stripe_says_unpaid(order: Order) -> bool:
    """Ask Stripe to cancel the intent, and treat its refusal as the source of truth.

    Cancelling also closes the door behind us: an intent left open could still be confirmed
    by a client that comes back later, producing a charge for an order we have released and a
    piece that now belongs to someone else.
    """
    if not order.payment_reference or not stripe_configured():
        # No intent was ever created, so there is nothing that could have been paid.
        return True
    try:
        get_stripe().PaymentIntent.cancel(order.payment_reference)
        return True
    except Exception as exc:
        # Stripe refuses to cancel an intent that is processing or succeeded, which is
        # exactly the case we must not touch. Anything else (a network blip) also lands
        # here, and leaving the order for the next pass is the safe way to be wrong.
        logger.info(
            "Leaving order %s reserved: Stripe would not cancel %s (%s).",
            order.id, order.payment_reference, exc,
        )
        return False


def expire_abandoned_orders() -> int:
    """Cancel unpaid orders past the window and put their pieces back on sale."""
    db = SessionLocal()
    released = 0
    try:
        cutoff = datetime.now(timezone.utc) - ABANDONED_AFTER
        candidates = db.execute(
            select(Order.id).where(
                Order.status == "pending_payment",
                Order.created_at < cutoff,
            )
        ).scalars().all()

        for order_id in candidates:
            # Re-read under a row lock. This sweep races the payment webhook for the same
            # order, and the status may have changed since the query above.
            order = orders_dao.get_order_for_update(db, order_id)
            if not order or order.status != "pending_payment":
                db.rollback()
                continue
            if not _stripe_says_unpaid(order):
                db.rollback()
                continue

            orders_dao.transition_order(db, order, "cancelled", commit=False)
            orders_dao.release_pieces(db, order, commit=False)
            db.commit()
            released += 1
            logger.info(
                "Released abandoned order %s (%s cents) back to the market.",
                order.id, order.total_cents,
            )
        return released
    finally:
        db.close()
