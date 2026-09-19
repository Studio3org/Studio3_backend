"""Settling an auction on a winner, and passing it down when one falls through.

The hard part of closing an auction is not picking the highest bid — it is what happens when
that bidder's card fails. Everything here exists to make that case safe.

**The invariant that makes a cascade possible.** A runner-up's money can only be fallen back
on while it is still authorised. So the two post-close states differ precisely in whose
money is held:

* ``awaiting_payment`` — the winner's capture failed. They have until ``winner_deadline_at``
  to fix it, and the runners-up holds are deliberately KEPT. Nothing has been taken from
  anyone. This is the only state a cascade can happen from.
* ``awaiting_winner`` — the capture succeeded, so the runners-up are released immediately.
  The sale is settled and only delivery details are outstanding. There is nothing to cascade
  to any more, and nothing that should be.

Releasing the runners-up at close in every case — which is what the first version did — is
what makes a cascade impossible: by the time the top bidder's payment is known to have
failed, the fallback has already been given their money back.

**Capture first, release second.** Within a single settlement the winner's hold is captured
before any losing hold is released. A release cannot be undone, so doing it the other way
round means a declined winner leaves an auction with no winner and no fallback.

**The windows come from the product decision**: forty-eight hours for a standalone auction,
ten minutes for an event one, because at an event the room empties and the piece has to
change hands that night.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from src.shared.models.auction import (
    AUCTION_AWAITING_PAYMENT,
    AUCTION_AWAITING_WINNER,
    AUCTION_NEEDS_SELLER_ACTION,
    Auction,
)
from src.shared.models.bid import BID_FORFEITED, BID_LOST, BID_WON, Bid
from src.shared.models.piece import Piece
from src.shared.utils.logger import get_logger
from src.modules.bids import bid_dao, holds_service
from src.modules.payments import money
from src.modules.pieces import piece_state

logger = get_logger(__name__)

# How long a winner has to fix a failed payment before the piece passes to the next bidder.
RETRY_WINDOW_STANDALONE = timedelta(hours=48)
# An event auction settles in the room. The client was explicit: notify both parties and
# retry on the spot, not two days later.
RETRY_WINDOW_EVENT = timedelta(minutes=10)

# How many bidders may be passed over before a human looks at it. Not a technical limit —
# an auction that has burned through four bidders has something wrong with it that another
# automatic attempt will not fix, and the seller should be the one to decide.
MAX_CASCADE_DEPTH = 3

# Outcomes of an attempt to settle on one bidder.
SETTLED = "settled"
PAYMENT_FAILED = "payment_failed"
EXHAUSTED = "exhausted"


def retry_window(auction: Auction) -> timedelta:
    return RETRY_WINDOW_EVENT if auction.is_event_auction else RETRY_WINDOW_STANDALONE


def winning_bid(db: Session, auction: Auction) -> Optional[Bid]:
    """The bid that won, read from the stored pointer.

    Deliberately not "the highest active bid". That query returns nothing once the close has
    marked the winner `won`, and it cannot express a cascade at all — after the top bidder
    forfeits, the winner is the second highest, which no ordering over amounts can say.
    """
    if auction.winning_bid_id is None:
        return None
    return db.get(Bid, auction.winning_bid_id)


def settle_on(
    db: Session,
    auction: Auction,
    piece: Optional[Piece],
    bid: Bid,
    *,
    commit: bool = False,
) -> str:
    """Try to settle this auction on one bidder. Returns SETTLED or PAYMENT_FAILED.

    Never raises on a declined card: a decline is an expected outcome of an auction, not an
    error, and the caller has to keep going either way.
    """
    now = datetime.now(timezone.utc)
    auction.winning_bid_id = bid.id
    hold = bid_dao.hold_for_bid(db, bid.id)

    if hold is None:
        # A bid with no hold cannot be settled on. Treated as a failed payment rather than a
        # crash so the cascade carries on to someone who can pay.
        logger.error("Bid %s on auction %s has no hold", bid.id, auction.id)
        return _park_for_payment(db, auction, piece, bid, now, commit=commit)

    try:
        holds_service.capture_hold(db, hold, commit=False)
    except Exception as exc:
        logger.warning(
            "Capture failed settling auction %s on bid %s: %s", auction.id, bid.id, exc
        )
        return _park_for_payment(db, auction, piece, bid, now, commit=commit)

    # The money is real. Book it before anything else can fail: a capture that reached Stripe
    # but never reached the ledger is money the platform holds and cannot account for.
    money.book_auction_captured(db, auction, hold, hold.capture_fee_cents or 0, commit=False)

    bid.status = BID_WON
    db.flush()  # autoflush is off; the read below must see this.
    # Only now, with the winner's money actually taken, is it safe to let the fallbacks go.
    _release_runners_up(db, auction, keep_bid_id=bid.id)

    auction.status = AUCTION_AWAITING_WINNER
    auction.closed_at = auction.closed_at or now
    # No deadline: the sale is settled and the money is in. What remains is delivery details,
    # and there is no honest way to "expire" a sale we have already taken payment for.
    auction.winner_deadline_at = None
    _move_piece_off_market(db, auction, piece)

    if commit:
        db.commit()
    logger.info("Auction %s settled on bid %s (%d cents)", auction.id, bid.id, bid.amount_cents)
    return SETTLED


def _park_for_payment(
    db: Session,
    auction: Auction,
    piece: Optional[Piece],
    bid: Bid,
    now: datetime,
    *,
    commit: bool,
) -> str:
    """The winner's payment failed. Give them a window, and keep the fallbacks funded.

    The bid stays `active`: they have not lost, and they have not forfeited yet. Nothing is
    released — the runners-up holds are the entire reason a cascade is possible.
    """
    auction.status = AUCTION_AWAITING_PAYMENT
    auction.closed_at = auction.closed_at or now
    auction.winner_deadline_at = now + retry_window(auction)
    _move_piece_off_market(db, auction, piece)
    if commit:
        db.commit()
    return PAYMENT_FAILED


def _move_piece_off_market(db: Session, auction: Auction, piece: Optional[Piece]) -> None:
    """A closed auction's piece is no longer for sale, whoever ends up paying for it.

    Idempotent: a cascade runs this again on a piece already moved, and transition_piece
    treats an unchanged status as a no-op.
    """
    if piece is None:
        return
    piece_state.transition_piece(
        db, piece, piece_state.AUCTION_WON,
        allowed_from={piece_state.LIVE, piece_state.AUCTION_WON},
        reason="auction_closed_with_winner", commit=False,
    )


def _release_runners_up(db: Session, auction: Auction, *, keep_bid_id: uuid.UUID) -> list[Bid]:
    """Mark every other live bid lost and give its money back."""
    released = []
    for other in bid_dao.ranked_active_bids(db, auction.id):
        if other.id == keep_bid_id:
            continue
        other.status = BID_LOST
        hold = bid_dao.hold_for_bid(db, other.id)
        if hold:
            holds_service.release_hold(db, hold, "auction_lost", commit=False)
        released.append(other)
    return released


def award_top_bidder(
    db: Session, auction: Auction, piece: Optional[Piece], *, commit: bool = False
) -> str:
    """Settle a just-closed auction on its highest bidder. Called by the close sweep."""
    top = bid_dao.get_highest_bid(db, auction.id)
    if top is None:
        return EXHAUSTED
    return settle_on(db, auction, piece, top, commit=commit)


def cascade(
    db: Session,
    auction: Auction,
    piece: Optional[Piece],
    *,
    reason: str,
    commit: bool = False,
) -> str:
    """The current winner is out. Pass the piece to the next bidder, or give up.

    Only legal from ``awaiting_payment``, and that is the point: it is the one state in which
    the runners-up still have money authorised. Calling this after a settled sale would
    "cascade" onto bidders whose holds were released at close, and every capture would fail.

    Returns SETTLED, PAYMENT_FAILED (the next bidder's card failed too — they now have their
    own window) or EXHAUSTED (nobody left, or too many attempts; parked for the seller).
    """
    now = datetime.now(timezone.utc)
    current = winning_bid(db, auction)
    if current is not None:
        current.status = BID_FORFEITED
        current.cancelled_at = now
        current.cancellation_reason = reason[:64]
        hold = bid_dao.hold_for_bid(db, current.id)
        if hold:
            holds_service.release_hold(db, hold, reason, commit=False)

    auction.cascade_depth = (auction.cascade_depth or 0) + 1
    auction.winning_bid_id = None
    # Sessions here run with autoflush off, so the forfeit above is still sitting in the
    # identity map. Without this flush the very next get_highest_bid() re-reads the database
    # and hands back the bidder who just forfeited — the cascade then "falls back" onto the
    # same person forever, capturing a hold it has already released.
    db.flush()

    if auction.cascade_depth > MAX_CASCADE_DEPTH:
        logger.warning(
            "Auction %s exhausted the cascade after %d attempts", auction.id, auction.cascade_depth
        )
        return _give_up(db, auction, piece, "cascade_exhausted", commit=commit)

    # get_highest_bid filters on `active`, and the bidder just forfeited was moved off it —
    # so this is the next bidder down without needing to exclude anyone by hand.
    nxt = bid_dao.get_highest_bid(db, auction.id)
    if nxt is None:
        return _give_up(db, auction, piece, "no_remaining_bidders", commit=commit)

    logger.info(
        "Auction %s cascading to bid %s (attempt %d)", auction.id, nxt.id, auction.cascade_depth
    )
    return settle_on(db, auction, piece, nxt, commit=commit)


def _give_up(
    db: Session, auction: Auction, piece: Optional[Piece], reason: str, *, commit: bool
) -> str:
    """Nobody left who can pay. Release everything and hand it to the seller.

    Deliberately not an auto-relist: the client chose to have a human decide what happens to
    a piece whose auction did not produce a sale.
    """
    bid_dao.cancel_active_bids(db, auction.id, reason=reason, commit=False)
    auction.status = AUCTION_NEEDS_SELLER_ACTION
    auction.winning_bid_id = None
    auction.winner_deadline_at = None
    auction.closed_at = auction.closed_at or datetime.now(timezone.utc)
    if piece is not None and piece.status == piece_state.AUCTION_WON:
        piece_state.transition_piece(
            db, piece, piece_state.DELISTED, allowed_from={piece_state.AUCTION_WON},
            reason=reason, commit=False,
        )
    if commit:
        db.commit()
    return EXHAUSTED
