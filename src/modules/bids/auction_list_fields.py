"""Auction fields on piece lists (BACKEND_CHANGES B21).

`SavedFolder` "Auction ends in 6h", `SavedAvailable` "Bid $1,300 · 6h left": lists serialise
pieces with `piece_to_dict`, which has the listing type but nothing about the auction, so an
auction piece read as Available at its starting price in feeds, profile grids and Saved.

`fields_for` loads the auctions and their bid totals for a whole list in two queries, keyed by
piece id, with the same field names as the detail endpoint's bid summary, so the app reads them
the same way. Only what every viewer may see: no viewer-specific fields (`isHighestBidder`,
`isWinner`, `awaitingPayment`), because lists are shared and cached, and never the reserve
amount (met / not met only, as on the detail). A fixed-price piece gets nothing.
"""
import uuid
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.shared.config.auction_config import bid_increment_cents
from src.shared.models.auction import (
    AUCTION_RUNNING_STATUSES,
    AUCTION_SETTLING_STATUSES,
    Auction,
)
from src.shared.models.bid import BID_ACTIVE, Bid
from src.shared.models.piece import Piece

# Running beats settling, as on the detail: a piece relisted while an old run settles shows
# the new run.
_RANK = {status: 0 for status in AUCTION_RUNNING_STATUSES} | {
    status: 1 for status in AUCTION_SETTLING_STATUSES
}


def _iso(value):
    return value.isoformat() if value else None


def fields_for(db: Session, pieces: Iterable[Piece]) -> dict[uuid.UUID, dict]:
    """`{piece_id: {auctionId, auctionStatus, startingBidCents, highestBidCents, bidCount,
    minNextBidCents, opensAt, auctionEndsAt, hasReserve, reserveMet, winningBidCents}}` for the
    auction pieces among `pieces`."""
    ids = [p.id for p in pieces if p is not None and p.listing_type == "auction"]
    if not ids:
        return {}
    chosen: dict[uuid.UUID, Auction] = {}
    for auction in db.execute(
        select(Auction).where(
            Auction.piece_id.in_(ids),
            Auction.status.in_(AUCTION_RUNNING_STATUSES + AUCTION_SETTLING_STATUSES),
        )
    ).scalars():
        current = chosen.get(auction.piece_id)
        if current is None or _RANK[auction.status] < _RANK[current.status]:
            chosen[auction.piece_id] = auction
    if not chosen:
        return {}

    auction_ids = [a.id for a in chosen.values()]
    totals = {
        auction_id: (highest, count)
        for auction_id, highest, count in db.execute(
            select(Bid.auction_id, func.max(Bid.amount_cents), func.count(Bid.id))
            .where(Bid.auction_id.in_(auction_ids), Bid.status == BID_ACTIVE)
            .group_by(Bid.auction_id)
        ).all()
    }
    winning_ids = [a.winning_bid_id for a in chosen.values() if a.winning_bid_id]
    winning = (
        dict(db.execute(select(Bid.id, Bid.amount_cents).where(Bid.id.in_(winning_ids))).all())
        if winning_ids
        else {}
    )

    result = {}
    for piece_id, auction in chosen.items():
        highest, count = totals.get(auction.id, (None, 0))
        result[piece_id] = {
            "auctionId": str(auction.id),
            "auctionStatus": auction.status,
            "startingBidCents": auction.starting_bid_cents,
            "highestBidCents": highest,
            "bidCount": count,
            # As bid_dao.min_next_bid_cents: the starting bid exactly until someone has bid.
            "minNextBidCents": (
                highest + bid_increment_cents(highest)
                if highest is not None
                else auction.starting_bid_cents
            ),
            "opensAt": _iso(auction.opens_at),
            "auctionEndsAt": _iso(auction.closes_at),
            "hasReserve": auction.reserve_cents is not None,
            "reserveMet": (
                highest is not None and highest >= auction.reserve_cents
                if auction.reserve_cents is not None
                else True
            ),
            "winningBidCents": winning.get(auction.winning_bid_id),
        }
    return result


def add_to(db: Session, pairs: Iterable[tuple[Piece, dict]]) -> None:
    """Merge the auction fields into each piece's dict, in place."""
    pairs = [(piece, d) for piece, d in pairs if piece is not None and d is not None]
    fields = fields_for(db, [piece for piece, _ in pairs])
    for piece, d in pairs:
        extra = fields.get(piece.id)
        if extra:
            d.update(extra)
