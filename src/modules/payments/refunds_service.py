"""The one way money goes back to a buyer.

Every refund — an admin resolving a dispute, an artist cancelling a paid order, a payment
that landed after its order was already cancelled — comes through here, so the rules below
hold for all of them instead of being re-derived (and forgotten) at each call site.

**Three steps, because Stripe sits in the middle.** A Stripe call must never run inside a
held row lock, and a refund must never race the artist's payout:

1. Under the order and payout locks: check the refund is allowed, **block the payout**, and
   record a `pending` refund row per payment. Commit. From here the payout cannot be
   released by any route (`payouts_service.RELEASABLE_STATUSES` excludes blocked), so the
   buyer being confirmed-received in the next second changes nothing.
2. No lock: ask Stripe for each refund, keyed on the payment, and record the outcome.
3. Under the lock again: mark the order refunded, put the piece back, book the ledger.

A Stripe failure stops after step 2 with the refund row `failed` and the payout still
blocked. It is never unblocked automatically — a network error may well have refunded the
buyer — so it waits for an admin to retry, which is safe: the same payment can only be
refunded once, and Stripe says so if it already was.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.config.stripe_client import get_stripe, stripe_configured
from src.shared.models.dispute import DISPUTE_RESOLVED_REFUND, Dispute
from src.shared.models.order import Order
from src.shared.models.payout import (
    PAYOUT_BLOCKED,
    PAYOUT_READY_TO_RELEASE,
    PAYOUT_RELEASED,
    Payout,
)
from src.shared.models.refund import (
    LEG_CHARGE,
    LEG_LATE,
    LEG_PREPAID,
    REFUND_FAILED,
    REFUND_PENDING,
    REFUND_SUCCEEDED,
    ROLE_ADMIN,
    ROLE_SELLER,
    ROLE_SYSTEM,
    Refund,
)
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.orders import orders_dao
from src.modules.payments import money

logger = get_logger(__name__)

# While the platform still holds the money. `completed` is absent: its payout is released
# straight away, and a released payout is in the artist's account, beyond this path.
REFUNDABLE_ORDER_STATUSES = ("paid", "shipped", "awaiting_confirmation", "disputed")
# An artist can only call off a sale they have not sent. Once it ships, an admin decides.
SELLER_CANCELLABLE_STATUSES = ("paid",)

PAYOUT_BLOCKED_REASON = "Refund in progress; payout held."
PAYOUT_REFUNDED_REASON = "Order refunded; payout cancelled."


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _is_dev_reference(reference: Optional[str]) -> bool:
    return not reference or reference.startswith("dev")


def _legs(order: Order) -> list[tuple[str, str, int]]:
    """(leg, payment reference, amount) for every payment the order was paid with.

    An auction order was paid twice — the hammer price when the auction closed, then the
    balance at checkout — and a refund of the order has to return both.
    """
    prepaid = order.prepaid_cents or 0
    reference = order.payment_reference or order.stripe_charge_id or f"dev:{order.id}"
    legs = [(LEG_CHARGE, reference, order.total_cents - prepaid)]
    if prepaid and order.prepaid_reference:
        legs.append((LEG_PREPAID, order.prepaid_reference, prepaid))
    return legs


def _already_refunded(error: Exception) -> bool:
    """Stripe's answer to refunding a payment twice. For us that is success: the money is
    back with the buyer, which is all a retry was trying to make true."""
    code = getattr(error, "code", None) or ""
    return code == "charge_already_refunded" or "already been refunded" in str(error)


def _stripe_refund(refund: Refund, order: Optional[Order], charge_id: Optional[str]) -> str:
    """Issue one refund at Stripe and return its id. Raises on failure."""
    stripe = get_stripe()
    target = (
        {"charge": charge_id}
        if refund.leg == LEG_CHARGE and charge_id
        else {"payment_intent": refund.payment_intent_id}
    )
    result = stripe.Refund.create(
        **target,
        reason="requested_by_customer",
        metadata={
            "order_id": str(order.id) if order else "",
            "leg": refund.leg,
            "initiated_by": refund.initiated_by_role,
        },
        # Keyed on the payment, not the order: there is only one full refund of a payment,
        # and an order with two payments (an auction win) needs two distinct keys.
        idempotency_key=f"refund:{refund.payment_intent_id}",
    )
    return result.get("id") if isinstance(result, dict) else getattr(result, "id", None)


def _send(refund_id: uuid.UUID, order_id: uuid.UUID, charge_id: Optional[str]) -> Optional[str]:
    """Step 2 for one refund row. Returns a failure message, or None when the money is back."""
    db = SessionLocal()
    try:
        refund = db.get(Refund, refund_id)
        if refund.status == REFUND_SUCCEEDED:
            return None
        order = db.get(Order, order_id) if order_id else None
        if not stripe_configured() or _is_dev_reference(refund.payment_intent_id):
            # Dev mode, or a dev-mode payment: nothing was charged, so nothing goes back.
            refund.status = REFUND_SUCCEEDED
            refund.failure_reason = None
            db.commit()
            return None
        try:
            refund.stripe_refund_id = _stripe_refund(refund, order, charge_id)
            refund.status = REFUND_SUCCEEDED
            refund.failure_reason = None
        except Exception as exc:  # noqa: BLE001 — anything Stripe raises is a failed refund
            if _already_refunded(exc):
                refund.status = REFUND_SUCCEEDED
                refund.failure_reason = None
            else:
                logger.exception("Refund %s (%s) failed at Stripe", refund.id, refund.leg)
                refund.status = REFUND_FAILED
                refund.failure_reason = str(exc)[:2000]
        db.commit()
        return refund.failure_reason if refund.status == REFUND_FAILED else None
    finally:
        db.close()


def refund_paid_order(
    order_id: uuid.UUID,
    *,
    actor_id: Optional[uuid.UUID],
    actor_role: str,
    reason: str,
) -> dict:
    """Refund a paid order in full: both payments if it was an auction win.

    `actor_role` is `admin` (any refundable status) or `seller` (only before it ships).
    Safe to call again after a failure — it picks up the refunds still outstanding.
    """
    # --- step 1: decide, block the payout, record the refunds -----------------------------
    db = SessionLocal()
    try:
        order = orders_dao.get_order_for_update(db, order_id)
        if not order:
            raise AppError("Order not found.", 404)
        if order.status == "refunded":
            return {"status": "already_refunded", "amountCents": order.total_cents}
        allowed = SELLER_CANCELLABLE_STATUSES if actor_role == ROLE_SELLER else REFUNDABLE_ORDER_STATUSES
        if order.status not in allowed:
            if actor_role == ROLE_SELLER:
                raise AppError(
                    "This order has already shipped. Contact support to cancel it.", 409
                )
            raise AppError(f"Can't refund an order in status '{order.status}'.", 409)

        payout = db.execute(
            select(Payout).where(Payout.order_id == order.id).with_for_update()
        ).scalar_one_or_none()
        if payout and payout.status in (PAYOUT_RELEASED, PAYOUT_READY_TO_RELEASE):
            raise AppError(
                "The artist has already been paid for this order; a refund here would leave "
                "the platform out of pocket. Recover the transfer in Stripe first.",
                409,
            )
        if payout and payout.status != PAYOUT_BLOCKED:
            payout.status = PAYOUT_BLOCKED
            payout.failure_reason = PAYOUT_BLOCKED_REASON

        plan = []
        for leg, reference, amount in _legs(order):
            refund = db.execute(
                select(Refund).where(Refund.payment_intent_id == reference)
            ).scalar_one_or_none()
            if refund is None:
                refund = Refund(
                    order_id=order.id,
                    payment_intent_id=reference,
                    leg=leg,
                    amount_cents=amount,
                    initiated_by_id=actor_id,
                    initiated_by_role=actor_role,
                    reason=reason,
                )
                db.add(refund)
                db.flush()
            elif refund.status == REFUND_FAILED:
                refund.status = REFUND_PENDING
            plan.append(refund.id)
        charge_id = order.stripe_charge_id
        db.commit()
    finally:
        db.close()

    # --- step 2: Stripe, with no lock held ------------------------------------------------
    failures = [f for f in (_send(rid, order_id, charge_id) for rid in plan) if f]
    if failures:
        logger.error("Refund for order %s failed at Stripe: %s", order_id, failures[0])
        if actor_role == ROLE_SELLER:
            # The artist can't open the admin console; support retries it from there.
            raise AppError(
                "We couldn't refund the buyer just now. The order is on hold and our team "
                "will finish the refund — you don't need to do anything.",
                502,
            )
        raise AppError(
            "The refund didn't go through at Stripe. The artist's payout is on hold; "
            f"retry the refund from the admin console. ({failures[0]})",
            502,
        )

    # --- step 3: the order follows the money ----------------------------------------------
    db = SessionLocal()
    try:
        order = orders_dao.get_order_for_update(db, order_id)
        if order.status != "refunded":
            orders_dao.transition_order(db, order, "refunded", commit=False)
            orders_dao.release_pieces(db, order, commit=False)
            money.book_refund_issued(db, order, commit=False)
        payout = db.execute(
            select(Payout).where(Payout.order_id == order.id)
        ).scalar_one_or_none()
        if payout:
            payout.status = PAYOUT_BLOCKED
            payout.failure_reason = PAYOUT_REFUNDED_REASON
        dispute = db.execute(
            select(Dispute).where(Dispute.order_id == order.id)
        ).scalar_one_or_none()
        if dispute and dispute.status != DISPUTE_RESOLVED_REFUND:
            dispute.status = DISPUTE_RESOLVED_REFUND
            dispute.resolution_note = reason
            dispute.resolved_at = _now()
            if actor_role == ROLE_ADMIN:
                dispute.resolved_by_admin_id = actor_id
        db.commit()
        _notify_refunded(db, order, actor_role)
        logger.info("Order %s refunded by %s %s: %s", order.id, actor_role, actor_id, reason)
        return {"status": "refunded", "amountCents": order.total_cents}
    finally:
        db.close()


def refund_late_payment(db, order, intent: dict, stripe_fee_cents: int) -> None:
    """A payment that succeeded after its order was cancelled or failed (PAY-2).

    Called from the payment webhook inside its own transaction. The order is not revived —
    its piece may already be someone else's — the money simply goes straight back. Raises on
    a Stripe failure so the webhook answers 5xx and Stripe redelivers; the refund row and the
    idempotency key make the redelivery safe.
    """
    _refund_late(db, order, intent, stripe_fee_cents, ticket=False)


def refund_late_ticket_payment(db, ticket_order, intent: dict, stripe_fee_cents: int) -> None:
    """The same for event tickets: a payment for a checkout whose hold already expired and
    whose tickets have gone to someone else."""
    _refund_late(db, ticket_order, intent, stripe_fee_cents, ticket=True)


def _refund_late(db, order, intent: dict, stripe_fee_cents: int, *, ticket: bool) -> None:
    intent_id = intent["id"]
    refund = db.execute(
        select(Refund).where(Refund.payment_intent_id == intent_id)
    ).scalar_one_or_none()
    if refund is not None and refund.status == REFUND_SUCCEEDED:
        return
    if refund is None:
        refund = Refund(
            order_id=None if ticket else order.id,
            ticket_order_id=order.id if ticket else None,
            payment_intent_id=intent_id,
            leg=LEG_LATE,
            amount_cents=int(intent.get("amount_received") or intent.get("amount") or 0),
            initiated_by_role=ROLE_SYSTEM,
            reason=f"Payment arrived after the order was {order.status}.",
        )
        db.add(refund)
        db.flush()
    try:
        refund.stripe_refund_id = _stripe_refund(refund, order, None)
    except Exception as exc:  # noqa: BLE001
        if not _already_refunded(exc):
            refund.status = REFUND_FAILED
            refund.failure_reason = str(exc)[:2000]
            db.commit()
            raise
    refund.status = REFUND_SUCCEEDED
    refund.failure_reason = None
    money.book_late_payment_refunded(
        db, order, intent_id, stripe_fee_cents, commit=False,
        ledger_order_id=None if ticket else order.id,
    )
    logger.warning(
        "Refunded late payment %s on %s %s %s.",
        intent_id, order.status, "ticket order" if ticket else "order", order.id,
    )


def _notify_refunded(db, order: Order, actor_role: str) -> None:
    from src.modules.notifications import notifications_dao

    try:
        if actor_role == ROLE_SELLER:
            notifications_dao.create_and_push(
                db, user_id=order.buyer_id, type="order", actor_id=None,
                target_type="order", target_id=order.id, payload={},
                title="Order cancelled",
                body="The artist cancelled your order and you've been refunded in full. "
                     "It may take a few days to appear.",
            )
        else:
            notifications_dao.create_and_push(
                db, user_id=order.buyer_id, type="order", actor_id=None,
                target_type="order", target_id=order.id, payload={},
                title="Refund issued",
                body="We've refunded your order in full. It may take a few days to appear.",
            )
    except Exception as exc:  # noqa: BLE001 — a refund must not fail on a notification
        logger.warning("Refund notification failed for order %s: %s", order.id, exc)
