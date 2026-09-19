"""Authorization holds — the money behind a bid.

A hold is a manual-capture PaymentIntent: funds reserved on the bidder's card but not taken.
Deliberately a separate Stripe code path from ordinary checkout, which auto-captures.

Three properties this file exists to maintain:

* **A bidder is never left unprotected.** Raising your own bid places the new hold and
  confirms it *before* releasing the old one. If the new authorisation fails, the old one
  still stands and the bid is unchanged.
* **One authorisation per bid, ever.** Deterministic idempotency keys plus a unique index on
  the intent id. A retried request cannot put two holds on one card.
* **The deadline comes from Stripe.** `capture_before` is read off the charge, never assumed
  to be seven days — it varies by network and the refresh sweep depends on the real value.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.config.stripe_client import get_stripe, platform_currency, stripe_configured
from src.shared.models.auction import (
    HOLD_CAPTURED,
    HOLD_FAILED,
    HOLD_HELD,
    HOLD_PENDING,
    HOLD_RELEASED,
    Hold,
)
from src.shared.models.bid import Bid
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)


class HoldError(AppError):
    """A hold could not be placed. User-facing: the bidder's card refused it."""


def ensure_customer(db: Session, user: User) -> str:
    """The Stripe customer a bidder's saved card belongs to.

    Needed because the refresh sweep re-authorises off-session, months of auction later, with
    nobody present to re-enter a card. A payment method has to be attached to a customer to
    be reusable at all.
    """
    if user.stripe_customer_id:
        return user.stripe_customer_id
    stripe = get_stripe()
    customer = stripe.Customer.create(
        email=user.email,
        metadata={"user_id": str(user.id)},
        idempotency_key=f"customer:{user.id}",
    )
    user.stripe_customer_id = customer["id"]
    db.flush()
    return customer["id"]


def _capture_deadline(intent: dict) -> Optional[datetime]:
    """When Stripe will stop letting us capture this authorisation.

    Read from the charge rather than assumed. Stripe's own guidance is to use this value:
    the window differs between card networks and between credit and debit, and the whole
    refresh mechanism is built on knowing the real deadline rather than a guessed one.
    """
    charge = intent.get("latest_charge")
    if isinstance(charge, str):
        try:
            charge = get_stripe().Charge.retrieve(charge)
        except Exception:
            logger.warning("Could not read capture_before for intent %s", intent.get("id"))
            return None
    if not isinstance(charge, dict):
        return None
    details = (charge.get("payment_method_details") or {}).get("card") or {}
    epoch = details.get("capture_before")
    if not epoch:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


def place_hold(
    db: Session,
    bid: Bid,
    bidder: User,
    amount_cents: int,
    payment_method_id: str,
    *,
    commit: bool = False,
) -> Hold:
    """Authorise `amount_cents` on the bidder's card for this bid.

    Per the product decision, a hold covers the **bid amount only**. Shipping and tax are
    taken at confirmation after the win, against this same saved card.
    """
    hold = Hold(
        id=uuid.uuid4(),
        bid_id=bid.id,
        bidder_id=bidder.id,
        amount_cents=amount_cents,
        status=HOLD_PENDING,
        stripe_payment_method_id=payment_method_id,
    )
    db.add(hold)
    db.flush()

    if not stripe_configured():
        # Dev mode: no keys. Treat the authorisation as granted so the auction lifecycle is
        # exercisable end to end without Stripe, exactly as checkout already does.
        hold.status = HOLD_HELD
        hold.stripe_payment_intent_id = f"dev_hold_{hold.id}"
        if commit:
            db.commit()
        return hold

    stripe = get_stripe()
    customer_id = ensure_customer(db, bidder)
    try:
        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency=platform_currency(),
            customer=customer_id,
            payment_method=payment_method_id,
            # The whole point: reserve the funds, take nothing yet.
            capture_method="manual",
            confirm=True,
            off_session=True,
            # Lets the refresh sweep re-authorise later without the bidder present.
            setup_future_usage="off_session",
            metadata={"bid_id": str(bid.id), "auction_id": str(bid.auction_id)},
            # Deterministic: a retried place_bid returns the same intent instead of a second
            # authorisation on the same card.
            idempotency_key=f"hold:{hold.id}",
        )
    except Exception as exc:
        hold.status = HOLD_FAILED
        hold.last_error = str(exc)[:1000]
        if commit:
            db.commit()
        logger.warning("Hold failed for bid %s: %s", bid.id, exc)
        raise HoldError(
            "Your card wouldn't authorise that amount. Try another card or a lower bid.", 402
        ) from exc

    hold.stripe_payment_intent_id = intent["id"]
    if intent.get("status") == "requires_capture":
        hold.status = HOLD_HELD
        hold.capture_before = _capture_deadline(intent)
    else:
        # Anything else means the authorisation is not usable — most often the issuer wanting
        # authentication we cannot obtain off-session.
        hold.status = HOLD_FAILED
        hold.last_error = f"Unexpected intent status {intent.get('status')!r}"
        if commit:
            db.commit()
        raise HoldError(
            "Your card needs confirming before it can be used to bid. Try adding it again.", 402
        )
    if commit:
        db.commit()
    return hold


def release_hold(db: Session, hold: Hold, reason: str, *, commit: bool = False) -> Hold:
    """Give the money back. Safe to call on an already-released hold."""
    if hold.status in (HOLD_RELEASED, HOLD_CAPTURED):
        return hold
    if stripe_configured() and hold.stripe_payment_intent_id and not hold.stripe_payment_intent_id.startswith("dev_"):
        try:
            get_stripe().PaymentIntent.cancel(
                hold.stripe_payment_intent_id, cancellation_reason="abandoned"
            )
        except Exception as exc:
            # An intent Stripe has already cancelled or expired raises here. The money is not
            # held either way, so recording the release is still correct — leaving it "held"
            # would make the refresh sweep keep re-authorising a dead bid.
            logger.warning("Release of hold %s reported: %s", hold.id, exc)
            hold.last_error = str(exc)[:1000]
    hold.status = HOLD_RELEASED
    hold.released_at = datetime.now(timezone.utc)
    logger.info("Released hold %s (%s)", hold.id, reason)
    if commit:
        db.commit()
    return hold


def capture_hold(db: Session, hold: Hold, *, commit: bool = False) -> Hold:
    """Take the money. The only hold transition that leads to a ledger entry."""
    if hold.status == HOLD_CAPTURED:
        return hold
    if hold.status != HOLD_HELD:
        raise AppError(f"Cannot capture a hold in state '{hold.status}'.", 409)

    if stripe_configured() and not (hold.stripe_payment_intent_id or "").startswith("dev_"):
        stripe = get_stripe()
        try:
            stripe.PaymentIntent.capture(
                hold.stripe_payment_intent_id,
                amount_to_capture=hold.amount_cents,
                idempotency_key=f"capture:{hold.id}",
            )
        except Exception as exc:
            hold.status = "capture_failed"
            hold.last_error = str(exc)[:1000]
            if commit:
                db.commit()
            logger.warning("Capture failed for hold %s: %s", hold.id, exc)
            raise HoldError("That card was declined when we tried to complete the sale.", 402) from exc

    hold.status = HOLD_CAPTURED
    hold.captured_at = datetime.now(timezone.utc)
    logger.info("Captured hold %s (%d cents)", hold.id, hold.amount_cents)
    if commit:
        db.commit()
    return hold


def live_hold_for_bidder(db: Session, auction_id: uuid.UUID, bidder_id: uuid.UUID) -> Optional[Hold]:
    """This bidder's current hold on this auction, if any.

    Used by the re-bid path: raising your own bid means placing a fresh hold and only then
    releasing this one.
    """
    from src.shared.models.auction import HOLD_LIVE_STATUSES

    return db.execute(
        select(Hold)
        .join(Bid, Bid.id == Hold.bid_id)
        .where(
            Bid.auction_id == auction_id,
            Hold.bidder_id == bidder_id,
            Hold.status.in_(HOLD_LIVE_STATUSES),
        )
        .order_by(Hold.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
