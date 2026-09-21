"""Bids — placement, and the reads the auction lifecycle depends on.

Every read filters on active bids. A cancelled auction's bids stay in the table as the
dispute record the spec requires, and counting them would resurrect them on a relist.

One thing worth being explicit about, because it is not obvious: **every bidder keeps their
hold until the auction closes.** Being outbid by someone else does not release your money —
you are still in the running if the higher bid falls through. The only thing that releases a
hold mid-auction is raising your *own* bid, which moves it to the new amount.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.shared.config.auction_config import bid_increment_cents
from src.shared.models.auction import (
    AUCTION_AWAITING_PAYMENT,
    AUCTION_CLOSING,
    AUCTION_LIVE,
    Auction,
    Hold,
)
from src.shared.models.bid import BID_ACTIVE, BID_CANCELLED, BID_OUTBID, Bid
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.bids import auction_dao, holds_service

logger = get_logger(__name__)

# Bids that still count: their money is committed and they could still win.
LIVE_BID_STATUSES = (BID_ACTIVE,)


def get_highest_bid(db: Session, auction_id: uuid.UUID) -> Optional[Bid]:
    """The leading bid. Ties break by who got there first."""
    return db.execute(
        select(Bid)
        .where(Bid.auction_id == auction_id, Bid.status.in_(LIVE_BID_STATUSES))
        .order_by(Bid.amount_cents.desc(), Bid.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()


def ranked_active_bids(db: Session, auction_id: uuid.UUID) -> list[Bid]:
    """Every live bid, highest first — the order the winner cascade walks."""
    return list(
        db.execute(
            select(Bid)
            .where(Bid.auction_id == auction_id, Bid.status.in_(LIVE_BID_STATUSES))
            .order_by(Bid.amount_cents.desc(), Bid.created_at.asc())
        ).scalars()
    )


def count_active_bids(db: Session, auction_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(Bid.id)).where(
            Bid.auction_id == auction_id, Bid.status.in_(LIVE_BID_STATUSES)
        )
    ).scalar_one()


def count_active_bids_for_piece(db: Session, piece_id: uuid.UUID) -> int:
    """Bids on whatever auction the piece is currently running, if any.

    Kept because the piece-level guards (delisting, editing terms) reason about the piece,
    not the auction.
    """
    auction = auction_dao.get_running_auction(db, piece_id)
    return count_active_bids(db, auction.id) if auction else 0


def list_bids_for_seller(db: Session, auction_id: uuid.UUID) -> list[Bid]:
    """Full history including cancelled bids. Seller-only: the spec is explicit that a seller
    sees who bid what on their own listing, and that bidders never see it about each other."""
    return list(
        db.execute(
            select(Bid)
            .where(Bid.auction_id == auction_id)
            .order_by(Bid.amount_cents.desc(), Bid.created_at.asc())
        ).scalars()
    )


def hold_for_bid(db: Session, bid_id: uuid.UUID) -> Optional[Hold]:
    return db.execute(select(Hold).where(Hold.bid_id == bid_id)).scalar_one_or_none()


def cancel_active_bids(
    db: Session, auction_id: uuid.UUID, reason: str, commit: bool = True
) -> list[Bid]:
    """Void every live bid and release the money behind it.

    Used when an auction is cancelled or ends without a sale. Releasing here rather than
    leaving it to the caller means no path can void a bid and forget its hold.
    """
    bids = ranked_active_bids(db, auction_id)
    now = datetime.now(timezone.utc)
    for bid in bids:
        bid.status = BID_CANCELLED
        bid.cancelled_at = now
        bid.cancellation_reason = reason[:64]
        hold = hold_for_bid(db, bid.id)
        if hold:
            holds_service.release_hold(db, hold, reason, commit=False)
    if commit:
        db.commit()
    else:
        db.flush()
    return bids


def min_next_bid_cents(db: Session, auction: Auction, highest: Optional[Bid] = None) -> int:
    """The lowest amount a new bid may be.

    With no bids yet this is the artist's starting bid EXACTLY. The increment only applies to
    a bid that has to beat an existing one — otherwise the number the artist advertised as
    their minimum would be the one amount nobody could bid.
    """
    if highest is None:
        highest = get_highest_bid(db, auction.id)
    if highest is None:
        return auction.starting_bid_cents
    return highest.amount_cents + bid_increment_cents(highest.amount_cents)


def bid_summary(db: Session, auction: Optional[Auction], viewer_id: Optional[uuid.UUID] = None) -> dict:
    """The auction as a bidder may see it.

    The reserve amount is never included — only whether it has been met. That is the whole
    point of a hidden reserve, and it would be trivial to leak by returning the number.
    """
    if auction is None:
        return {}
    highest = get_highest_bid(db, auction.id)
    highest_cents = highest.amount_cents if highest else None
    winner = db.get(Bid, auction.winning_bid_id) if auction.winning_bid_id else None
    return {
        "auctionId": str(auction.id),
        "auctionStatus": auction.status,
        "startingBidCents": auction.starting_bid_cents,
        "highestBidCents": highest_cents,
        "bidCount": count_active_bids(db, auction.id),
        "minNextBidCents": min_next_bid_cents(db, auction, highest=highest),
        "bidIncrementCents": bid_increment_cents(highest_cents) if highest_cents else 0,
        "opensAt": auction.opens_at.isoformat() if auction.opens_at else None,
        "auctionEndsAt": auction.closes_at.isoformat() if auction.closes_at else None,
        "hasReserve": auction.reserve_cents is not None,
        # Met / not met only. Never the number.
        "reserveMet": (
            highest_cents is not None and highest_cents >= auction.reserve_cents
            if auction.reserve_cents is not None
            else True
        ),
        "deliveryMode": auction.delivery_mode,
        "isHighestBidder": bool(highest and viewer_id and highest.bidder_id == viewer_id),
        # Post-close state. `highest` is the highest *active* bid and goes empty the moment
        # the close marks a winner, so none of this can be derived from it — which is exactly
        # why the winner is stored on the auction.
        "winnerDeadlineAt": (
            auction.winner_deadline_at.isoformat() if auction.winner_deadline_at else None
        ),
        "isWinner": bool(
            viewer_id and winner is not None and winner.bidder_id == viewer_id
        ),
        # Drives the "your card was declined, add another" prompt. Only the winner is told —
        # a losing bidder has no business knowing the winner's payment failed, and telling
        # them would leak that the piece may be about to become available again.
        "awaitingPayment": (
            auction.status == AUCTION_AWAITING_PAYMENT
            and bool(viewer_id and winner is not None and winner.bidder_id == viewer_id)
        ),
        "winningBidCents": winner.amount_cents if winner else None,
    }


def place_bid(
    db: Session,
    auction_id: uuid.UUID,
    bidder: User,
    amount_cents: int,
    payment_method_id: Optional[str] = None,
) -> Bid:
    """Place a bid and authorise the money behind it.

    The ordering here is the point of the function. When a bidder raises their own bid we
    place and confirm the new hold *first*, and only then release the old one — so a card
    that refuses the new amount leaves the original hold, and the original bid, intact. Doing
    it the other way round would drop a bidder out of an auction they were winning.
    """
    # Lock the auction so two concurrent bids serialise rather than both reading the same
    # high bid and both clearing the minimum.
    auction = db.execute(
        select(Auction).where(Auction.id == auction_id).with_for_update()
    ).scalar_one_or_none()
    if not auction:
        raise AppError("Auction not found.", 404)
    if auction.status not in (AUCTION_LIVE, AUCTION_CLOSING):
        raise AppError("This auction is not open for bidding.", 409)
    if auction.seller_id == bidder.id:
        raise AppError("You can't bid on your own piece.", 400)

    now = datetime.now(timezone.utc)
    if auction.opens_at and now < auction.opens_at:
        raise AppError("Bidding hasn't opened for this auction yet.", 409)
    if auction.closes_at and now >= auction.closes_at:
        raise AppError("Bidding has closed for this piece.", 409)

    minimum = min_next_bid_cents(db, auction)
    if amount_cents < minimum:
        raise AppError(f"Bid must be at least ${minimum / 100:.2f}.", 409)

    previous_hold = holds_service.live_hold_for_bidder(db, auction.id, bidder.id)
    if payment_method_id is None and previous_hold is not None:
        # Re-bidding: reuse the card already on this auction rather than asking again.
        payment_method_id = previous_hold.stripe_payment_method_id
    if payment_method_id is None:
        raise AppError("A payment method is required to bid.", 400)

    bid = Bid(
        id=uuid.uuid4(),
        auction_id=auction.id,
        bidder_id=bidder.id,
        amount_cents=amount_cents,
        status=BID_ACTIVE,
    )
    db.add(bid)
    db.flush()

    # Place and confirm the new authorisation before touching the old one. If this raises,
    # nothing above has been committed and the previous hold still stands.
    holds_service.place_hold(
        db, bid, bidder, amount_cents, payment_method_id, commit=False
    )

    if previous_hold is not None:
        previous_bid = db.get(Bid, previous_hold.bid_id)
        if previous_bid is not None:
            previous_bid.status = BID_OUTBID
        holds_service.release_hold(db, previous_hold, "raised_own_bid", commit=False)

    # A bid in the closing minutes pushes the end out, so nobody wins by sniping.
    auction_dao.apply_soft_close(db, auction, now)

    db.commit()
    db.refresh(bid)
    logger.info(
        "Bid %s placed on auction %s (%d cents)", bid.id, auction.id, amount_cents
    )
    return bid
