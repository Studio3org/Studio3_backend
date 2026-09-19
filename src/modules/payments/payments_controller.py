"""Payment intent creation and webhook entry point."""
import uuid

from flask import g, request

from src.shared.config.database import SessionLocal
from src.shared.config.stripe_client import get_stripe, platform_currency, stripe_configured
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.shared.utils.request_id import request_id
from src.modules.orders import orders_dao
from src.modules.payments import webhook_handler

logger = get_logger(__name__)


def create_payment_intent(order_id: str):
    """Create (or reuse) the PaymentIntent for an order and return its client secret.

    Idempotent by design: calling twice returns the same intent rather than creating a
    second one, so a retried checkout can't produce two charges for one order.
    """
    if not stripe_configured():
        raise AppError("Payments are not configured.", 503)

    db = SessionLocal()
    try:
        buyer_id = uuid.UUID(g.user["id"])
        order = orders_dao.get_order_for_update(db, uuid.UUID(order_id))
        if not order or order.buyer_id != buyer_id:
            raise AppError("Order not found.", 404)
        if order.status != "pending_payment":
            raise AppError("This order has already been processed.", 409)

        # What is actually left to collect. An auction win has already had its hammer price
        # captured from the winner's hold at close, so charging total_cents here would take
        # the artwork a second time.
        balance_cents = order.total_cents - (order.prepaid_cents or 0)
        if balance_cents <= 0:
            raise AppError("This order has already been paid in full.", 409)

        stripe = get_stripe()

        if order.payment_reference:
            intent = stripe.PaymentIntent.retrieve(order.payment_reference)
            if intent.get("status") not in ("canceled", "succeeded"):
                return {
                    "clientSecret": intent["client_secret"],
                    "paymentIntentId": intent["id"],
                    "amountCents": balance_cents,
                }, 200

        intent = stripe.PaymentIntent.create(
            amount=balance_cents,
            currency=platform_currency(),
            # transfer_group set at creation links this charge to the later payout transfer,
            # and is what makes crash recovery possible (we can find an orphaned transfer).
            transfer_group=f"order_{order.id}",
            # order_id in metadata means the webhook can find the order even if our
            # payment_reference write lost a race with Stripe's delivery.
            metadata={"order_id": str(order.id), "buyer_id": str(order.buyer_id)},
            automatic_payment_methods={"enabled": True},
            idempotency_key=f"pi:{order.id}",
        )
        order.payment_provider = "stripe"
        order.payment_reference = intent["id"]
        db.commit()

        return {
            "clientSecret": intent["client_secret"],
            "paymentIntentId": intent["id"],
            "amountCents": balance_cents,
        }, 201
    finally:
        db.close()


def payment_status(order_id: str):
    """Read-only poll for the client after Stripe's client-side confirm returns.

    Deliberately does not transition anything: the webhook is the only thing that marks an
    order paid (NFR-1). The client polls this until it sees the webhook's result.
    """
    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        order = orders_dao.get_order(db, uuid.UUID(order_id))
        if not order or (order.buyer_id != user_id and order.seller_id != user_id):
            raise AppError("Order not found.", 404)
        return {
            "orderId": str(order.id),
            "status": order.status,
            "paid": order.status not in ("pending_payment", "failed", "cancelled"),
        }, 200
    finally:
        db.close()


def webhook():
    """Stripe webhook endpoint. No auth decorator — authenticity comes from the signature.

    Returns 2xx quickly for anything already handled; genuine processing failures return
    5xx so Stripe retries.
    """
    payload = request.get_data()  # raw bytes — get_json() would break signature verification
    signature = request.headers.get("Stripe-Signature", "")
    event = webhook_handler.construct_event(payload, signature)

    # One id for the whole event. It is handled across three separate sessions, and without
    # this its log lines cannot be told apart from a concurrent delivery's.
    with request_id(f"evt:{event['id']}"):
        return _process_event(event)


def _process_event(event):
    db = SessionLocal()
    try:
        if not webhook_handler.record_event(db, event):
            return {"received": True, "duplicate": True}, 200
    finally:
        db.close()

    try:
        webhook_handler.handle_event(event)
    except Exception as e:
        logger.exception("Failed handling Stripe event %s: %s", event["id"], e)
        db = SessionLocal()
        try:
            webhook_handler._mark_processed(db, event["id"], error=str(e))
        finally:
            db.close()
        # 500 so Stripe retries — the dedup guard makes the retry safe.
        raise AppError("Webhook processing failed.", 500, is_operational=False)

    db = SessionLocal()
    try:
        webhook_handler._mark_processed(db, event["id"])
    finally:
        db.close()
    return {"received": True}, 200
