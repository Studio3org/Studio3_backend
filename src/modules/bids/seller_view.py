"""What the artist sees on their own auction (BACKEND_CHANGES B38).

`ArtistAuctionLive` lists every bid, newest first, with the bidders as "Bidder 1", "Bidder 2":
the same person keeps the same number in one auction, numbered by when they first bid. Nobody
is named, the artist included, until the winner pays. `ArtistAuctionEnd` says how many people
are told ("all 4 bidders"), and `ArtistAuctionPayment` how much the next bidder would pay.

`ArtistAuctionUnsold` needs the run that just ended. The piece detail only carries a running or
settling auction, so once one closed without a sale the artist's page had nothing to say why;
`last_auction` is that run, for the artist only.

The run is the same one `my_bid` uses: the running auction, else the one being settled, else
the most recent one.
"""
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.modules.bids import auction_dao, my_bid
from src.shared.models.auction import Auction
from src.shared.models.bid import Bid


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _numbers(bids: list) -> dict:
    """Bidder number per bidder id, by when each first bid (ties by amount)."""
    numbers: dict = {}
    for bid in sorted(bids, key=lambda b: (b.created_at, b.amount_cents)):
        numbers.setdefault(bid.bidder_id, len(numbers) + 1)
    return numbers


def _top_bidder(auction: Auction, bids: list) -> Optional[uuid.UUID]:
    """The winner once there is one, else whoever bid the most."""
    if getattr(auction, "winning_bid_id", None):
        for bid in bids:
            if bid.id == auction.winning_bid_id:
                return bid.bidder_id
    if not bids:
        return None
    return max(bids, key=lambda b: (b.amount_cents, -b.created_at.timestamp())).bidder_id


def history(auction: Auction, bids: list) -> dict:
    """Every bid in the run, newest first, with anonymous bidder numbers.

    `nextBidCents` is the most anyone other than the top bidder (the winner once there is one)
    offered: what a second chance would be at.
    """
    numbers = _numbers(bids)
    top = _top_bidder(auction, bids)
    others = [b.amount_cents for b in bids if b.bidder_id != top]
    newest = sorted(bids, key=lambda b: (b.created_at, b.amount_cents), reverse=True)
    return {
        "auctionId": str(auction.id),
        "auctionStatus": auction.status,
        "bidCount": len(bids),
        "bidderCount": len(numbers),
        "nextBidCents": max(others) if others else None,
        "items": [
            {
                "bidder": numbers[b.bidder_id],
                "amountCents": b.amount_cents,
                "createdAt": _iso(b.created_at),
                "isTop": b.bidder_id == top,
            }
            for b in newest
        ],
    }


def summary(auction: Auction, bids: list) -> dict:
    """The run that ended, for the artist's own page (`ArtistAuctionUnsold`).

    The bid summary's highest bid counts live bids only, and a close without a sale voids them
    all, so the top bid here is read from every bid in the run.
    """
    return {
        "auctionId": str(auction.id),
        "status": auction.status,
        "startingBidCents": auction.starting_bid_cents,
        "reserveCents": auction.reserve_cents,
        "topBidCents": max((b.amount_cents for b in bids), default=None),
        "bidCount": len(bids),
        "bidderCount": len({b.bidder_id for b in bids}),
        "closedAt": _iso(getattr(auction, "closed_at", None)),
    }


def _bids(db: Session, auction_id: uuid.UUID) -> list:
    return list(db.execute(select(Bid).where(Bid.auction_id == auction_id)).scalars())


def bid_history(db: Session, piece_id: uuid.UUID) -> dict:
    """`GET /api/pieces/<id>/auction/bids` for the piece's artist."""
    auction = my_bid._this_run(db, piece_id)
    if auction is None:
        return {
            "auctionId": None,
            "auctionStatus": None,
            "bidCount": 0,
            "bidderCount": 0,
            "nextBidCents": None,
            "items": [],
        }
    return history(auction, _bids(db, auction.id))


def last_auction(db: Session, piece_id: uuid.UUID) -> Optional[dict]:
    """The most recent run that is over (sold, unsold or cancelled), when none is running or
    being settled; None otherwise."""
    if auction_dao.get_running_auction(db, piece_id) or auction_dao.get_settling_auction(
        db, piece_id
    ):
        return None
    auction = my_bid._this_run(db, piece_id)
    if auction is None:
        return None
    return summary(auction, _bids(db, auction.id))
