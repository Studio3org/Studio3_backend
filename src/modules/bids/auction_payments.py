"""Auctions v2 money (Phase 7a, the 9 Nov design; BACKEND_CHANGES B20).

* **First bid: a card check, not a hold.** The bidder saves a card with a SetupIntent (the
  existing `/payments/setup-intent`), and the bid only checks that the card belongs to them.
  Nothing is reserved; only the winner is ever charged.
* **The charge at close.** The winner's saved card is charged off-session for the hammer
  price (Phase 7b adds shipping, insurance and tax). The charge is recorded on a `Hold` row in
  the `captured` state, so everything downstream — the escrow booking, the auction checkout's
  `prepaid_cents`, refunds — reads it exactly as it reads a captured hold. A bank check (SCA)
  counts as a failed payment: nobody is there to answer it.
* **Card fails:** the winner has 12 hours to fix it (a new card, or the automatic retries at
  1, 4 and 8 hours). The window is set once and never moves (BE-4).
* **Second chance:** then the next bidder whose top bid met the reserve is offered the piece
  at that amount, for 24 hours. Accepting charges their saved card at once; declining or
  letting it lapse passes it on. Nobody left: the seller decides (needs_seller_action).

Escrow is kept: every charge lands on the platform account, never as a destination charge,
and the artist is paid by Transfer after delivery (decision A2).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.config import auction_config
from src.shared.config.stripe_client import get_stripe, platform_currency, stripe_configured
from src.shared.models.auction import (
    HOLD_CAPTURED,
    HOLD_FAILED,
    HOLD_PENDING,
    OFFER_ACCEPTED,
    OFFER_DECLINED,
    OFFER_EXPIRED,
    OFFER_PENDING,
    Auction,
    AuctionOffer,
    Hold,
)
from src.shared.models.bid import BID_ACTIVE, Bid
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

# Stripe's test payment method that always declines; in development (no Stripe keys) this
# id declines too, so the decline paths can be tried locally.
DEV_DECLINED_CARD = "pm_card_chargeDeclined"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- the card check at the first bid ------------------------------------------------------------

def check_card(db: Session, bidder: User, payment_method_id: str) -> str:
    """The card is the bidder's own and saved for later charges. Returns it.

    Saving goes through a SetupIntent (`POST /api/payments/setup-intent`), which is where the
    bank verifies it. Here we only refuse a card that isn't attached to this bidder: charging
    it off-session at close would fail, and too late to tell them."""
    if not payment_method_id:
        raise AppError("Add a card to bid. It's only charged if you win.", 400)
    if not stripe_configured() or payment_method_id.startswith("dev_"):
        return payment_method_id
    from src.modules.bids.holds_service import ensure_customer

    customer_id = ensure_customer(db, bidder)
    try:
        method = get_stripe().PaymentMethod.retrieve(payment_method_id)
    except Exception as exc:  # noqa: BLE001
        raise AppError("We couldn't find that card. Add it again and try once more.", 400) from exc
    owner = method.get("customer") if isinstance(method, dict) else getattr(method, "customer", None)
    if owner != customer_id:
        raise AppError("Save this card first (it's checked once, then only charged if you win).", 400)
    return payment_method_id


# --- charging the winner ---------------------------------------------------------------------------

def _hold_for(db: Session, bid: Bid) -> Optional[Hold]:
    return db.execute(select(Hold).where(Hold.bid_id == bid.id)).scalar_one_or_none()


def charge_bid(db: Session, auction: Auction, bid: Bid, *, payment_method_id: Optional[str] = None,
               attempt: Optional[int] = None) -> tuple[bool, Optional[str]]:
    """Charge this bid's saved card for its amount. Returns (paid, error message).

    Inside the caller's transaction, which holds the auction lock. Stripe is called here
    under that lock on purpose: a close or a retry is one bidder, one charge, and the
    deterministic idempotency key makes a repeat return the same PaymentIntent."""
    from src.modules.bids.holds_service import _capture_fee_cents, ensure_customer
    from src.modules.payments import money

    hold = _hold_for(db, bid)
    # The card: the one just given, the bid's saved card, or (an auction from before 7a)
    # the card behind its old hold.
    method = payment_method_id or bid.payment_method_id or (
        hold.stripe_payment_method_id if hold is not None else None)
    if hold is not None and hold.status == HOLD_CAPTURED:
        return True, None
    if hold is None:
        hold = Hold(id=uuid.uuid4(), bid_id=bid.id, bidder_id=bid.bidder_id,
                    amount_cents=bid.amount_cents, status=HOLD_PENDING,
                    stripe_payment_method_id=method)
        db.add(hold)
        db.flush()
    hold.stripe_payment_method_id = method
    attempt = attempt if attempt is not None else (auction.payment_attempts or 0)

    if not method:
        hold.status = HOLD_FAILED
        hold.last_error = "No saved card on this bid."
        return False, hold.last_error

    if not stripe_configured() or method.startswith("dev_"):
        if method == DEV_DECLINED_CARD:
            # Development without Stripe: Stripe's own test card for a decline, so the
            # fix-window and second-chance flows can be run end to end locally.
            hold.status = HOLD_FAILED
            hold.last_error = "Your card was declined. (development test card)"
            return False, "The card was declined."
        hold.stripe_payment_intent_id = hold.stripe_payment_intent_id or f"dev_charge_{hold.id}"
        fee = 0
    else:
        bidder = db.get(User, bid.bidder_id)
        try:
            intent = get_stripe().PaymentIntent.create(
                amount=bid.amount_cents,
                currency=platform_currency(),
                customer=ensure_customer(db, bidder),
                payment_method=method,
                confirm=True,
                off_session=True,
                metadata={"kind": "auction_charge", "auction_id": str(auction.id),
                          "bid_id": str(bid.id)},
                idempotency_key=f"auction_charge:{bid.id}:{attempt}:{method}",
            )
        except Exception as exc:  # noqa: BLE001 — a decline (or a bank check) is a failure
            hold.status = HOLD_FAILED
            hold.last_error = str(exc)[:1000]
            logger.info("Auction %s: charge on bid %s failed: %s", auction.id, bid.id, exc)
            return False, _friendly(exc)
        hold.stripe_payment_intent_id = intent["id"]
        if intent.get("status") != "succeeded":
            hold.status = HOLD_FAILED
            hold.last_error = f"Payment needs action ({intent.get('status')})"
            try:
                get_stripe().PaymentIntent.cancel(intent["id"])
            except Exception:  # noqa: BLE001
                pass
            return False, "Your bank asked to confirm the payment, and we couldn't do that for you."
        fee = _capture_fee_cents(intent)

    hold.status = HOLD_CAPTURED
    hold.captured_at = _now()
    hold.capture_fee_cents = fee
    hold.last_error = None
    db.flush()
    # The money is real: escrow it before anything else can go wrong (as a hold capture).
    money.book_auction_captured(db, auction, hold, fee, commit=False)
    return True, None


def _friendly(exc: Exception) -> str:
    code = getattr(exc, "code", None) or ""
    if code in ("authentication_required",):
        return "Your bank asked to confirm the payment, and we couldn't do that for you."
    if code in ("insufficient_funds",):
        return "The card didn't have enough funds."
    if code in ("expired_card",):
        return "The card has expired."
    return "The card was declined."


def park_failed(auction: Auction, now: Optional[datetime] = None) -> None:
    """After a failed charge: start the fix window if it isn't running (and never extend it —
    BE-4), count the attempt and line up the next automatic retry."""
    now = now or _now()
    if auction.winner_deadline_at is None:
        auction.winner_deadline_at = now + timedelta(hours=auction_config.payment_fix_hours())
    auction.payment_attempts = (auction.payment_attempts or 0) + 1
    started = auction.winner_deadline_at - timedelta(hours=auction_config.payment_fix_hours())
    upcoming = [started + timedelta(hours=h) for h in auction_config.PAYMENT_RETRY_AFTER_HOURS]
    previous = auction.next_payment_retry_at
    upcoming = [t for t in upcoming if now < t < auction.winner_deadline_at
                and (previous is None or t > previous)]
    auction.next_payment_retry_at = upcoming[0] if upcoming else None


# --- second chance -----------------------------------------------------------------------------------

def next_candidate(db: Session, auction: Auction) -> Optional[Bid]:
    """The next bidder down whose top bid met the reserve, and who hasn't had an offer."""
    offered = set(db.execute(select(AuctionOffer.bid_id).where(AuctionOffer.auction_id == auction.id)).scalars())
    seen_bidders = set()
    for bid in db.execute(
        select(Bid).where(Bid.auction_id == auction.id, Bid.status == BID_ACTIVE)
        .order_by(Bid.amount_cents.desc(), Bid.created_at.asc())
    ).scalars():
        if bid.bidder_id in seen_bidders:
            continue
        seen_bidders.add(bid.bidder_id)
        if bid.id in offered or bid.id == auction.winning_bid_id:
            continue
        if auction.reserve_cents is not None and bid.amount_cents < auction.reserve_cents:
            return None
        return bid
    return None


def make_offer(db: Session, auction: Auction, bid: Bid) -> AuctionOffer:
    offer = AuctionOffer(id=uuid.uuid4(), auction_id=auction.id, bid_id=bid.id,
                         bidder_id=bid.bidder_id, amount_cents=bid.amount_cents,
                         status=OFFER_PENDING,
                         expires_at=_now() + timedelta(hours=auction_config.second_chance_hours()))
    db.add(offer)
    db.flush()
    return offer


def pending_offer(db: Session, auction_id: uuid.UUID, *, lock: bool = False) -> Optional[AuctionOffer]:
    stmt = select(AuctionOffer).where(AuctionOffer.auction_id == auction_id,
                                      AuctionOffer.status == OFFER_PENDING)
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalars().first()


def close_offer(offer: AuctionOffer, status: str) -> None:
    assert status in (OFFER_ACCEPTED, OFFER_DECLINED, OFFER_EXPIRED)
    offer.status = status
    offer.responded_at = _now()
