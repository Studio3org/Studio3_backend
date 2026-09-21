"""Stripe webhook processing — the only place order payment status is decided.

Three properties this file must maintain:

* **Signature-verified.** An unsigned or badly-signed payload is rejected before anything
  is read from it (NFR-3).
* **Idempotent.** Stripe retries deliveries. Every event is recorded in
  stripe_webhook_events keyed on the Stripe event id; a duplicate returns early.
* **Order-tolerant.** Stripe does not guarantee delivery order, so handlers check current
  state instead of assuming a sequence. `transition_order` treats a no-op transition as
  success, which is what makes out-of-order delivery safe.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.shared.config.database import SessionLocal
from src.shared.config.stripe_client import get_stripe, payouts_ready, webhook_secrets
from src.shared.models.order import Order
from src.shared.models.payout import (
    PAYOUT_BLOCKED,
    PAYOUT_PENDING,
    PAYOUT_TRANSFER_FAILED,
    Payout,
)
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.models.webhook_event import StripeWebhookEvent
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.orders import orders_dao
from src.modules.pieces import piece_state
from src.modules.payments import money
from src.modules.notifications import notifications_dao

logger = get_logger(__name__)

# Events we act on. Anything else is still recorded for audit, then ignored.
HANDLED_EVENTS = {
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "charge.refunded",
    "charge.dispute.created",
    "charge.dispute.closed",
    "account.updated",
    "transfer.created",
    "transfer.failed",
    "transfer.reversed",
}


def construct_event(payload: bytes, signature: str):
    """Verify the signature and return the parsed event. Raises AppError(400) on failure —
    never trust an unverified payload."""
    secrets = webhook_secrets()
    if not secrets:
        raise AppError("Webhook secret is not configured.", 500)
    stripe = get_stripe()
    # Try each configured secret: the account endpoint and the Connect endpoint share
    # this URL but sign with different secrets.
    for secret in secrets:
        try:
            return stripe.Webhook.construct_event(payload, signature, secret)
        except ValueError:
            raise AppError("Invalid webhook payload.", 400)
        except stripe.error.SignatureVerificationError:
            continue
    logger.warning("Rejected Stripe webhook: signature matched none of the configured secrets.")
    raise AppError("Invalid webhook signature.", 400)


def record_event(db: Session, event) -> bool:
    """Record the event for audit and report whether it should be handled now.

    False means "already fully processed, skip". A row that exists but never reached
    `processed_at` means the previous attempt raised and Stripe is retrying, and those MUST
    be handled: answering those "duplicate" is how a single transient error (a Charge
    retrieve timing out) left an order paid at Stripe but pending_payment here, with no
    ledger entry and no payout row, permanently.

    Two simultaneous deliveries of the same event can both see a null processed_at and both
    proceed. That is tolerable because every handler is independently idempotent — the
    ledger keys on idempotency_key, transition_order no-ops on a repeat, and the payout
    insert checks for an existing row.
    """
    row = StripeWebhookEvent(
        id=uuid.uuid4(),
        stripe_event_id=event["id"],
        event_type=event["type"],
        payload=dict(event),
    )
    db.add(row)
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(StripeWebhookEvent).where(
                StripeWebhookEvent.stripe_event_id == event["id"]
            )
        ).scalar_one_or_none()
        if existing is not None and existing.processed_at is None:
            logger.info(
                "Stripe event %s was recorded but never processed; handling this retry.",
                event["id"],
            )
            return True
        logger.info("Duplicate Stripe event %s ignored.", event["id"])
        return False


def _mark_processed(db: Session, stripe_event_id: str, error: str = None) -> None:
    row = db.execute(
        StripeWebhookEvent.__table__.select().where(
            StripeWebhookEvent.stripe_event_id == stripe_event_id
        )
    ).first()
    if not row:
        return
    obj = db.get(StripeWebhookEvent, row.id)
    if obj:
        if error:
            obj.error = error
        else:
            obj.processed_at = datetime.now(timezone.utc)
        db.commit()


def handle_event(event) -> None:
    """Dispatch a verified, newly-recorded event. Raises on failure so the caller can
    return 5xx and let Stripe retry."""
    event_type = event["type"]
    if event_type not in HANDLED_EVENTS:
        logger.info("Ignoring unhandled Stripe event type %s.", event_type)
        return

    db = SessionLocal()
    try:
        obj = event["data"]["object"]
        if event_type == "payment_intent.succeeded":
            _on_payment_succeeded(db, obj)
        elif event_type == "payment_intent.payment_failed":
            _on_payment_failed(db, obj)
        elif event_type == "charge.refunded":
            _on_charge_refunded(db, obj)
        elif event_type == "charge.dispute.created":
            _on_chargeback_opened(db, obj)
        elif event_type == "charge.dispute.closed":
            _on_chargeback_closed(db, obj)
        elif event_type == "account.updated":
            _on_account_updated(db, obj)
        elif event_type == "transfer.created":
            _on_transfer_created(db, obj)
        elif event_type == "transfer.failed":
            _on_transfer_failed(db, obj)
        elif event_type == "transfer.reversed":
            _on_transfer_reversed(db, obj)
    finally:
        db.close()


# --- payment lifecycle -----------------------------------------------------------------

def _order_for_intent(db: Session, intent) -> Order:
    order_id = (intent.get("metadata") or {}).get("order_id")
    order = None
    if order_id:
        try:
            order = orders_dao.get_order_for_update(db, uuid.UUID(order_id))
        except ValueError:
            order = None
    if not order:
        order = orders_dao.get_order_by_payment_reference(db, intent["id"])
        if order:
            order = orders_dao.get_order_for_update(db, order.id)
    return order


def _stripe_fee_for_charge(charge) -> int:
    """The exact Stripe fee from the charge's balance_transaction.

    Expanding it is worth the extra call: an estimated fee would leave platform_clearing
    permanently diverged from the real Stripe balance, which is exactly what the ledger
    exists to prevent.
    """
    balance_txn = charge.get("balance_transaction")
    if isinstance(balance_txn, dict):
        return int(balance_txn.get("fee", 0))
    if isinstance(balance_txn, str):
        stripe = get_stripe()
        return int(stripe.BalanceTransaction.retrieve(balance_txn).get("fee", 0))
    return 0


def _on_payment_succeeded(db: Session, intent) -> None:
    order = _order_for_intent(db, intent)
    if not order:
        logger.error("payment_intent.succeeded for unknown order: %s", intent["id"])
        return
    if order.status not in ("pending_payment", "paid"):
        logger.warning(
            "Ignoring payment_intent.succeeded for order %s in status %s.",
            order.id, order.status,
        )
        return

    stripe = get_stripe()
    charge_id = intent.get("latest_charge")
    charge = stripe.Charge.retrieve(charge_id, expand=["balance_transaction"]) if charge_id else None
    fee = _stripe_fee_for_charge(charge) if charge else 0

    already_paid = order.status == "paid"
    order.payment_provider = "stripe"
    order.payment_reference = intent["id"]
    if charge_id:
        order.stripe_charge_id = charge_id
    if not already_paid:
        orders_dao.transition_order(db, order, "paid", commit=False)
        for item in orders_dao.list_items(db, order.id):
            piece = db.get(Piece, item.piece_id)
            if piece:
                piece_state.transition_piece(
                    db, piece, piece_state.SOLD,
                    allowed_from={piece_state.RESERVED}, commit=False,
                )

    # Payout row is created here, at capture time, so the amount owed exists in the ledger
    # from the moment the money arrives — release just executes it later.
    existing_payout = db.execute(
        Payout.__table__.select().where(Payout.order_id == order.id)
    ).first()
    if not existing_payout:
        db.add(
            Payout(
                id=uuid.uuid4(),
                order_id=order.id,
                seller_id=order.seller_id,
                status=PAYOUT_PENDING,
                idempotency_key=f"payout:{order.id}",
            )
        )

    money.book_order_paid(db, order, stripe_fee_cents=fee, commit=False)
    db.commit()
    logger.info("Order %s paid (fee %d cents).", order.id, fee)

    if not already_paid:
        _notify_paid(db, order)


def _notify_paid(db: Session, order: Order) -> None:
    buyer = db.get(User, order.buyer_id)
    items = orders_dao.list_items(db, order.id)
    piece = db.get(Piece, items[0].piece_id) if items else None
    title = piece.title if piece else None
    try:
        notifications_dao.create_and_push(
            db,
            user_id=order.seller_id,
            type="purchase",
            actor_id=order.buyer_id,
            target_type="order",
            target_id=order.id,
            payload={"pieceTitle": title, "totalCents": order.total_cents},
            title="You made a sale!",
            body=f"{buyer.name} purchased '{title}'" if title else f"{buyer.name} completed a purchase",
        )
        notifications_dao.create_and_push(
            db,
            user_id=order.buyer_id,
            type="purchase",
            actor_id=order.seller_id,
            target_type="order",
            target_id=order.id,
            payload={"pieceTitle": title},
            title="Order confirmed",
            body=f"Your order for '{title}' is confirmed" if title else "Your order is confirmed",
        )
    except Exception as e:
        # A failed push must not fail the webhook — the money already moved.
        logger.warning("Notification failed for order %s: %s", order.id, e)


def _on_payment_failed(db: Session, intent) -> None:
    order = _order_for_intent(db, intent)
    if not order:
        logger.error("payment_intent.payment_failed for unknown order: %s", intent["id"])
        return
    if order.status != "pending_payment":
        return
    orders_dao.transition_order(db, order, "failed", commit=False)
    orders_dao.release_pieces(db, order, commit=False)
    db.commit()
    logger.info("Order %s failed; pieces returned to live.", order.id)


def _on_charge_refunded(db: Session, charge) -> None:
    """Stripe confirming a refund.

    Normally a no-op: our own admin refund books the ledger reversal and marks the order
    before Stripe ever calls back. What this really covers is a refund issued **by hand in
    the Stripe dashboard**, which nothing else would catch.

    And that is why it has to be careful. `book_refund_issued` reverses the *whole* order —
    the artist's share, the commission and the tax. Booking it against anything less than a
    complete refund puts a hole in the ledger the size of the difference, which nothing
    errors on: the books simply stop matching the money until the nightly reconciliation
    notices days later.

    Two ways a refund can be less than complete, and both used to slip through:

    * **A partial refund.** Stripe sends `charge.refunded` for these too — its own
      documentation says so on the event picker.
    * **One charge of an auction order.** An auction win is paid in two charges: the hammer
      price captured at close, and the balance at checkout. Refunding the balance charge in
      full is still only part of the order.

    Anything short of the full order total is therefore recorded for a human rather than
    guessed at. Apportioning a partial refund across commission, shipping and tax is a
    product decision nobody has made, and inventing one here would be worse than leaving it
    on the ops queue.
    """
    row = db.execute(
        Order.__table__.select().where(Order.stripe_charge_id == charge["id"])
    ).first()
    if not row:
        logger.warning("charge.refunded for unknown charge %s.", charge["id"])
        return
    order = orders_dao.get_order_for_update(db, row.id)
    if order.status == "refunded":
        # Our own refund path already booked it. This is the confirmation.
        return

    refunded_cents = int(charge.get("amount_refunded") or 0)
    # What this charge was for: the whole order, or only the balance left after an auction
    # captured the hammer price at close.
    prepaid = order.prepaid_cents or 0
    covers_whole_order = prepaid == 0 and refunded_cents >= order.total_cents

    if not covers_whole_order:
        _flag_incomplete_refund(db, order, charge, refunded_cents, prepaid)
        return

    orders_dao.transition_order(db, order, "refunded", commit=False)
    orders_dao.release_pieces(db, order, commit=False)
    money.book_refund_issued(db, order, stripe_fee_cents=_stripe_fee_for_charge(charge), commit=False)
    db.commit()
    logger.info("Order %s refunded (via Stripe).", order.id)


def _flag_incomplete_refund(db: Session, order, charge, refunded_cents: int, prepaid: int) -> None:
    """A refund that does not cover the whole order. Recorded, never booked.

    The order is deliberately left alone: its status, its piece and its ledger all still
    describe a sale that mostly happened, which is the truth until somebody decides what the
    partial refund means.
    """
    from src.modules.admin import audit_service
    from src.shared.models.audit import AUDIT_REFUND_NEEDS_REVIEW

    reason = (
        "an auction order is paid in two charges, so refunding one is only part of it"
        if prepaid
        else "the refund is less than the order total"
    )
    logger.error(
        "Partial refund on order %s (%d of %d cents refunded) — NOT booked: %s",
        order.id, refunded_cents, order.total_cents, reason,
    )
    audit_service.system(
        db,
        AUDIT_REFUND_NEEDS_REVIEW,
        subject_type="order",
        subject_id=order.id,
        detail={
            "refundedCents": refunded_cents,
            "orderTotalCents": order.total_cents,
            "prepaidCents": prepaid,
            "chargeId": charge.get("id"),
        },
        note=(
            f"Refunded outside the app and {reason}. The ledger has NOT been adjusted — "
            "decide how it should be apportioned before booking anything."
        ),
    )


# --- chargebacks -----------------------------------------------------------------------

def _on_chargeback_opened(db: Session, dispute) -> None:
    """A real card chargeback: the buyer's bank is pulling the money back. Distinct from an
    in-app Dispute. Payout must be blocked — paying the artist on money that gets clawed
    back means the platform eats the loss twice."""
    row = db.execute(
        Order.__table__.select().where(Order.stripe_charge_id == dispute.get("charge"))
    ).first()
    if not row:
        logger.warning("Chargeback for unknown charge %s.", dispute.get("charge"))
        return
    order = orders_dao.get_order_for_update(db, row.id)
    payout = db.execute(Payout.__table__.select().where(Payout.order_id == order.id)).first()
    if payout:
        obj = db.get(Payout, payout.id)
        if obj and obj.status == PAYOUT_PENDING:
            # BLOCKED, not TRANSFER_FAILED: the latter is in RELEASABLE_STATUSES because a
            # failed transfer is meant to be retryable, which would let this payout out.
            obj.status = PAYOUT_BLOCKED
            obj.failure_reason = (
                f"Blocked: card chargeback {dispute.get('id')} opened on this order."
            )
    db.commit()
    logger.error(
        "CHARGEBACK opened on order %s (%s) — payout blocked, needs ops review.",
        order.id, dispute.get("id"),
    )


def _on_chargeback_closed(db: Session, dispute) -> None:
    row = db.execute(
        Order.__table__.select().where(Order.stripe_charge_id == dispute.get("charge"))
    ).first()
    if not row:
        return
    won = dispute.get("status") == "won"
    payout = db.execute(Payout.__table__.select().where(Payout.order_id == row.id)).first()
    if payout and won:
        obj = db.get(Payout, payout.id)
        # Chargeback defended: the money is ours again, so the payout can be retried.
        if obj and obj.status == PAYOUT_BLOCKED:
            obj.status = PAYOUT_PENDING
            obj.failure_reason = None
    db.commit()
    logger.info("Chargeback %s closed (%s) on order %s.", dispute.get("id"),
                dispute.get("status"), row.id)


# --- connect + transfers ---------------------------------------------------------------

def _on_account_updated(db: Session, account) -> None:
    """Source of truth for whether an artist can receive payouts."""
    user = db.execute(
        User.__table__.select().where(User.stripe_account_id == account["id"])
    ).first()
    if not user:
        return
    obj = db.get(User, user.id)
    enabled = payouts_ready(account)
    if obj.stripe_payouts_enabled != enabled:
        obj.stripe_payouts_enabled = enabled
        db.commit()
        logger.info("Connect account %s payouts_enabled=%s.", account["id"], enabled)


def _payout_for_transfer(db: Session, transfer_id: str):
    row = db.execute(
        Payout.__table__.select().where(Payout.stripe_transfer_id == transfer_id)
    ).first()
    return db.get(Payout, row.id) if row else None


def _on_transfer_created(db: Session, transfer) -> None:
    logger.info("Transfer %s created (%s cents).", transfer["id"], transfer.get("amount"))


def _on_transfer_failed(db: Session, transfer) -> None:
    payout = _payout_for_transfer(db, transfer["id"])
    if not payout:
        logger.warning("transfer.failed for unknown transfer %s.", transfer["id"])
        return
    payout.status = PAYOUT_TRANSFER_FAILED
    payout.failure_reason = transfer.get("failure_message") or "Transfer failed at Stripe."
    db.commit()
    logger.error("Payout %s failed: %s", payout.id, payout.failure_reason)


def _on_transfer_reversed(db: Session, transfer) -> None:
    """A transfer clawed back after the fact — real money moved, so the ledger must record
    it and the artist is owed again."""
    payout = _payout_for_transfer(db, transfer["id"])
    if not payout:
        return
    order = orders_dao.get_order_for_update(db, payout.order_id)
    reversed_amount = int(transfer.get("amount_reversed") or 0)
    if order and reversed_amount:
        money.book_transfer_reversed(
            db, order, reversed_amount, reversal_id=transfer["id"], commit=False
        )
    payout.status = PAYOUT_TRANSFER_FAILED
    payout.failure_reason = "Transfer was reversed."
    db.commit()
    logger.error("Transfer %s reversed on order %s.", transfer["id"], payout.order_id)
