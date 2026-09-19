"""Auctions — creating them, opening them, and reading their public shape."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.config import commission as commission_policy
from src.shared.models.auction import (
    AUCTION_CLOSING,
    AUCTION_DRAFT,
    AUCTION_LIVE,
    AUCTION_RUNNING_STATUSES,
    AUCTION_SETTLING_STATUSES,
    Auction,
)
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.utils.app_error import AppError

MIN_DURATION_DAYS = 3
MAX_DURATION_DAYS = 14
# A bid inside this window pushes the close out by the same amount, repeatedly. Standalone
# auctions only — an event auction stops dead so the piece can change hands that night.
SOFT_CLOSE_WINDOW = timedelta(minutes=5)
# An event auction closes this far before the event ends, leaving time to settle and hand over.
EVENT_CLOSE_BUFFER = timedelta(minutes=30)


def get_running_auction(db: Session, piece_id: uuid.UUID, *, lock: bool = False) -> Optional[Auction]:
    """The auction currently attached to this piece, if any.

    A piece accumulates auction rows over time — cancel-then-relist is how a piece moves onto
    an event — so "the auction" always means the one that is running now.
    """
    stmt = select(Auction).where(
        Auction.piece_id == piece_id, Auction.status.in_(AUCTION_RUNNING_STATUSES)
    )
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def create_auction(
    db: Session,
    piece: Piece,
    seller: User,
    *,
    starting_bid_cents: int,
    duration_days: Optional[int] = None,
    reserve_cents: Optional[int] = None,
    event_id: Optional[uuid.UUID] = None,
    delivery_mode: Optional[str] = None,
    commit: bool = True,
) -> Auction:
    """Create the auction for a piece, in draft.

    Draft, not live: the clock starts when the piece is published, not when the artist fills
    in the form. Publishing a piece that has sat in draft for a week must not open an auction
    that is already a week old.
    """
    if get_running_auction(db, piece.id):
        raise AppError("This piece already has an auction running.", 409)
    if starting_bid_cents < 100:
        raise AppError("Starting bid must be at least $1.00.", 400)
    if reserve_cents is not None and reserve_cents < starting_bid_cents:
        raise AppError("A reserve cannot be below the starting bid.", 400)

    is_event = event_id is not None
    if not is_event:
        if duration_days is None:
            raise AppError("An auction needs a duration.", 400)
        if not MIN_DURATION_DAYS <= duration_days <= MAX_DURATION_DAYS:
            raise AppError(
                f"Auction duration must be between {MIN_DURATION_DAYS} and "
                f"{MAX_DURATION_DAYS} days.",
                400,
            )

    auction = Auction(
        id=uuid.uuid4(),
        piece_id=piece.id,
        seller_id=seller.id,
        status=AUCTION_DRAFT,
        starting_bid_cents=starting_bid_cents,
        reserve_cents=reserve_cents,
        duration_days=None if is_event else duration_days,
        # An event auction takes its window from the event and never extends: the room empties.
        soft_close_enabled=not is_event,
        event_id=event_id,
        delivery_mode=delivery_mode,
        # Snapshotted now so the net figure the artist was shown at listing is the one they
        # are paid, whatever the platform rate does in the meantime.
        commission_bps=commission_policy.resolve_bps(seller, commission_policy.SALE_ART),
    )
    db.add(auction)
    if commit:
        db.commit()
        db.refresh(auction)
    else:
        db.flush()
    return auction


def open_auction(
    db: Session,
    auction: Auction,
    *,
    opens_at: Optional[datetime] = None,
    closes_at: Optional[datetime] = None,
    commit: bool = True,
) -> Auction:
    """Start the clock. Called when the piece goes live.

    An event auction is handed its window explicitly — opens at the event's start, closes
    thirty minutes before it ends. A standalone auction computes its own from the duration.
    """
    if auction.status != AUCTION_DRAFT:
        raise AppError("This auction has already started.", 409)

    now = datetime.now(timezone.utc)
    auction.opens_at = opens_at or now
    if closes_at is not None:
        auction.closes_at = closes_at
    else:
        auction.closes_at = auction.opens_at + timedelta(days=auction.duration_days)
    if auction.closes_at <= auction.opens_at:
        raise AppError("An auction cannot close before it opens.", 400)

    auction.status = AUCTION_LIVE
    if commit:
        db.commit()
        db.refresh(auction)
    return auction


def extend_auction(db: Session, auction: Auction, extra_days: int, *, commit: bool = True) -> Auction:
    """The one manual extension a seller gets.

    Only more than three days out, so extending cannot be used to manipulate exactly when an
    auction ends. Never shorter, under any circumstance.
    """
    if auction.status not in (AUCTION_LIVE, AUCTION_CLOSING):
        raise AppError("Only a running auction can be extended.", 409)
    if auction.is_event_auction:
        raise AppError("An event auction's timing comes from the event.", 409)
    if auction.extended_once:
        raise AppError("This auction has already been extended once.", 409)
    if not 1 <= extra_days <= 3:
        raise AppError("An auction can be extended by up to 3 days.", 400)
    remaining = auction.closes_at - datetime.now(timezone.utc)
    if remaining <= timedelta(days=3):
        raise AppError(
            "An auction can only be extended more than 3 days before it closes.", 409
        )

    auction.closes_at = auction.closes_at + timedelta(days=extra_days)
    auction.extended_once = True
    if commit:
        db.commit()
        db.refresh(auction)
    return auction


def apply_soft_close(db: Session, auction: Auction, now: datetime) -> bool:
    """Push the close out if a bid landed in the final minutes. Returns whether it moved.

    Repeatable and unbounded by design — it exists to stop sniping, and a cap would just move
    the sniping to the cap. Event auctions opt out entirely.
    """
    if not auction.soft_close_enabled or auction.closes_at is None:
        return False
    if auction.closes_at - now > SOFT_CLOSE_WINDOW:
        return False
    auction.closes_at = now + SOFT_CLOSE_WINDOW
    auction.status = AUCTION_CLOSING
    return True


def get_settling_auction(
    db: Session, piece_id: uuid.UUID, *, lock: bool = False
) -> Optional[Auction]:
    """The closed-but-unfinished auction for this piece, if any.

    Separate from get_running_auction because a settling auction is past its close: no bid
    may be placed on it, but it is still the row checkout reads the winner and the captured
    amount from. Conflating the two would let a bid land on an auction that has already
    taken someone's money.
    """
    stmt = select(Auction).where(
        Auction.piece_id == piece_id, Auction.status.in_(AUCTION_SETTLING_STATUSES)
    )
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt.order_by(Auction.created_at.desc()).limit(1)).scalar_one_or_none()
