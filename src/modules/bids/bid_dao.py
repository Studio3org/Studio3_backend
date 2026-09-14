"""Bids DAO — auction bid placement and read helpers."""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.models.bid import Bid
from src.shared.models.piece import Piece
from src.shared.utils.app_error import AppError

# Flat increment shown in the design ("Minimum bid is $25 above the current highest") —
# not a percentage, so it doesn't need to scale with price.
MIN_BID_INCREMENT_CENTS = 2500


def get_highest_bid(db: Session, piece_id: uuid.UUID) -> Optional[Bid]:
    return db.execute(
        select(Bid)
        .where(Bid.piece_id == piece_id)
        .order_by(Bid.amount_cents.desc(), Bid.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()


def count_bids(db: Session, piece_id: uuid.UUID) -> int:
    from sqlalchemy import func

    return db.execute(select(func.count(Bid.id)).where(Bid.piece_id == piece_id)).scalar_one()


def min_next_bid_cents(db: Session, piece: Piece, highest: Optional[Bid] = None) -> int:
    if highest is None:
        highest = get_highest_bid(db, piece.id)
    base = highest.amount_cents if highest else (piece.price_cents or 0)
    return base + MIN_BID_INCREMENT_CENTS


def bid_summary(db: Session, piece: Piece, viewer_id: Optional[uuid.UUID] = None) -> dict:
    highest = get_highest_bid(db, piece.id)
    return {
        "highestBidCents": highest.amount_cents if highest else piece.price_cents,
        "bidCount": count_bids(db, piece.id),
        "minNextBidCents": min_next_bid_cents(db, piece, highest=highest),
        "auctionEndsAt": piece.auction_ends_at.isoformat() if piece.auction_ends_at else None,
        # Only meaningful once the auction has closed (status == "auction_won") — tells the
        # viewer's own client whether *they* won, without exposing the bidder's identity to
        # anyone else looking at the same piece.
        "isHighestBidder": bool(highest and viewer_id and highest.bidder_id == viewer_id),
    }


def place_bid(db: Session, piece_id: uuid.UUID, bidder_id: uuid.UUID, amount_cents: int) -> Bid:
    # Lock the piece row so two concurrent bids on the same piece serialize instead of both
    # reading the same "current highest" and both passing the minimum check — same technique
    # as orders_dao.create_order for checkout.
    piece = db.execute(
        select(Piece).where(Piece.id == piece_id).with_for_update()
    ).scalar_one_or_none()
    if not piece or piece.deleted_at is not None:
        raise AppError("Piece not found.", 404)
    if not piece.is_for_sale or piece.listing_type != "auction":
        raise AppError("This piece is not up for auction.", 400)
    if piece.status != "live":
        raise AppError("This piece is no longer available.", 409)
    if piece.auction_ends_at and piece.auction_ends_at <= datetime.now(timezone.utc):
        raise AppError("Bidding has closed for this piece.", 409)

    minimum = min_next_bid_cents(db, piece)
    if amount_cents < minimum:
        raise AppError(f"Bid must be at least ${minimum / 100:.2f}.", 409)

    bid = Bid(id=uuid.uuid4(), piece_id=piece.id, bidder_id=bidder_id, amount_cents=amount_cents)
    db.add(bid)
    db.commit()
    db.refresh(bid)
    return bid
