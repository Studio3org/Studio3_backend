"""The viewer's own bid on a piece (BACKEND_CHANGES B18).

`BidOutbid` shows Outbid with the next minimum; `BidLost` and `BidReserveNotMet` show "Your top
bid". The bid summary only said `isHighestBidder`, and nothing at all once an auction had
closed without a sale, so the app kept the viewer's bids on the phone they were placed from.
This answers from the bids themselves, from any phone, before and after the close:

* `myTopBidCents`: the most the viewer bid in the piece's current (or last) run, or null.
* `myBidStatus`:
  - `leading`: bidding is open and theirs is the highest live bid
  - `outbid`: bidding is open and someone is higher; or it closed and they are a runner-up still
    held in case the winner's payment fails
  - `won`: their bid won (paid or still being paid)
  - `lost`: it closed and they did not get it: someone else won, it ended below the reserve or
    was cancelled, or they won and did not pay
  - `none`: no bid in this run

"This run" is the running auction, else the one being settled, else the most recent one: a
cancelled run whose piece was listed again does not count against the new one.
"""
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.modules.bids import auction_dao, bid_dao
from src.shared.models.auction import AUCTION_CLOSING, AUCTION_DRAFT, AUCTION_LIVE, Auction
from src.shared.models.bid import BID_ACTIVE, BID_WON, Bid

LEADING = "leading"
OUTBID = "outbid"
WON = "won"
LOST = "lost"
NONE = "none"


def _this_run(db: Session, piece_id: uuid.UUID) -> Optional[Auction]:
    return (
        auction_dao.get_running_auction(db, piece_id)
        or auction_dao.get_settling_auction(db, piece_id)
        or db.execute(
            select(Auction)
            .where(Auction.piece_id == piece_id, Auction.status != AUCTION_DRAFT)
            .order_by(Auction.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    )


def status_for(auction: Auction, mine: list, highest: Optional[Bid], viewer_id: uuid.UUID) -> str:
    """The rules above, given the viewer's bids in the run and the highest live bid."""
    if not mine:
        return NONE
    if auction.status in (AUCTION_LIVE, AUCTION_CLOSING):
        return LEADING if highest is not None and highest.bidder_id == viewer_id else OUTBID
    statuses = {bid.status for bid in mine}
    if BID_WON in statuses:
        return WON
    if BID_ACTIVE in statuses:
        return OUTBID
    return LOST


def viewer_bid(db: Session, piece_id: uuid.UUID, viewer_id: Optional[uuid.UUID]) -> dict:
    """`myTopBidCents` and `myBidStatus` for a signed-in viewer; nothing for anyone else."""
    if viewer_id is None:
        return {}
    auction = _this_run(db, piece_id)
    mine = []
    if auction is not None:
        mine = list(
            db.execute(
                select(Bid).where(Bid.auction_id == auction.id, Bid.bidder_id == viewer_id)
            ).scalars()
        )
    highest = None
    if mine and auction.status in (AUCTION_LIVE, AUCTION_CLOSING):
        highest = bid_dao.get_highest_bid(db, auction.id)
    return {
        "myTopBidCents": max((bid.amount_cents for bid in mine), default=None),
        "myBidStatus": status_for(auction, mine, highest, viewer_id),
    }
