"""Saved cards — what bidding is built on.

Ordinary checkout can collect a card at the moment of payment, because the buyer is sitting
there. Bidding cannot: a hold is authorised when the bid is placed and **re-authorised weeks
later with nobody present**, so the card has to be saved against a Stripe customer up front
and referenced by id from then on.

That is why these endpoints exist and why they are not part of checkout. The client's
sequence is:

1. ``POST /setup-intent`` — get a SetupIntent plus an ephemeral key, hand both to Stripe's
   PaymentSheet, and let Stripe collect and vault the card.
2. ``GET /payment-methods`` — list what is saved, so a repeat bidder picks rather than
   retypes.
3. Pass the chosen ``paymentMethodId`` to ``POST /api/pieces/:id/bids``.

**No card data ever reaches this server.** The SetupIntent flow means the number goes from
the device straight to Stripe, and this app only ever sees an id and a last-four.
"""
import uuid

from flask import g

from src.shared.config.database import SessionLocal
from src.shared.config.stripe_client import get_stripe, stripe_configured
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.bids import holds_service
from src.modules.user.user_dao import get_user_by_id

logger = get_logger(__name__)

# Pinned rather than left to the library default. The ephemeral key is minted for a specific
# API version and Stripe's mobile SDK rejects a mismatch, so this has to move deliberately
# and in step with the app — not silently, when the server's stripe package is upgraded.
EPHEMERAL_KEY_API_VERSION = "2024-06-20"


def create_setup_intent():
    """Start saving a card for this user, without charging it.

    A SetupIntent rather than a PaymentIntent: nothing is owed yet. The card is being vaulted
    so a future bid can authorise against it, which is a different thing from taking money
    and must not be confused with one in the UI either.
    """
    if not stripe_configured():
        raise AppError("Payments are not configured.", 503)

    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        if user is None:
            raise AppError("User not found.", 404)

        stripe = get_stripe()
        customer_id = holds_service.ensure_customer(db, user)
        # Committed before the Stripe calls below: ensure_customer may have just created the
        # customer, and losing that id would orphan it and mint a second one on the retry.
        db.commit()

        ephemeral_key = stripe.EphemeralKey.create(
            customer=customer_id, stripe_version=EPHEMERAL_KEY_API_VERSION
        )
        intent = stripe.SetupIntent.create(
            customer=customer_id,
            # off_session is the whole point: the saved card is re-authorised by the refresh
            # sweep and captured at close, both with the bidder nowhere near their phone.
            usage="off_session",
            payment_method_types=["card"],
            metadata={"user_id": str(user.id)},
        )
        return {
            "clientSecret": intent["client_secret"],
            "setupIntentId": intent["id"],
            "customerId": customer_id,
            "ephemeralKeySecret": ephemeral_key["secret"],
        }, 201
    finally:
        db.close()


def list_payment_methods():
    """The cards this user has saved, for a bidder to pick from.

    Ids and last-fours only. There is nothing else here worth returning and plenty worth not
    returning.
    """
    if not stripe_configured():
        # Not an error: a build without Stripe configured should show an empty wallet rather
        # than a failure, so the rest of the app stays usable.
        return {"paymentMethods": []}, 200

    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        if user is None:
            raise AppError("User not found.", 404)
        if not user.stripe_customer_id:
            # Never bid, never saved a card. An empty list, not a 404.
            return {"paymentMethods": []}, 200

        methods = get_stripe().PaymentMethod.list(
            customer=user.stripe_customer_id, type="card"
        )
        return {"paymentMethods": [_card_dict(m) for m in methods.get("data", [])]}, 200
    finally:
        db.close()


def detach_payment_method(payment_method_id: str):
    """Forget a saved card.

    Ownership is checked against the caller's own customer before detaching — the id comes
    from the client, and without this check anyone could detach anyone else's card by
    guessing one.
    """
    if not stripe_configured():
        raise AppError("Payments are not configured.", 503)

    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        if user is None or not user.stripe_customer_id:
            raise AppError("Payment method not found.", 404)

        stripe = get_stripe()
        method = stripe.PaymentMethod.retrieve(payment_method_id)
        if method.get("customer") != user.stripe_customer_id:
            raise AppError("Payment method not found.", 404)

        # Deliberately not blocked when a live hold is using this card. Detaching does not
        # cancel an existing authorisation — that money stays held and capturable — and
        # refusing would leave someone unable to remove a card because of a bid they have
        # since been outbid on. The refresh sweep will fail on it and tell them, which is the
        # behaviour that already exists for an expired card.
        stripe.PaymentMethod.detach(payment_method_id)
        logger.info("Detached payment method for user %s", user.id)
        return {"detached": True}, 200
    finally:
        db.close()


def _card_dict(method: dict) -> dict:
    card = method.get("card") or {}
    return {
        "id": method.get("id"),
        "brand": card.get("brand"),
        "last4": card.get("last4"),
        "expMonth": card.get("exp_month"),
        "expYear": card.get("exp_year"),
    }
