"""What an account change does to the rest of the app (BACKEND_CHANGES B34, Settings).

* **Going public** approves every pending follow request, once, and tells each person.
* **Turning off selling** takes down fixed-price listings and auctions nobody has bid on (the
  pieces stay, not for sale). It is refused while an auction has bids or a sale is still
  going through: those are promises to other people.
* **Deleting an account** first asks `deletion_blockers`. The answer lists everything that
  has to finish before the account can go: an auction with bids (theirs, or one they're
  winning), a piece shipping to or from them, money still on its way to them, an upcoming
  event they host with tickets sold, card fees they owe. The delete refuses while the list
  isn't empty, so nobody is left holding a ticket, a bid or a parcel for an account that has
  gone.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.shared.models.auction import (
    AUCTION_RUNNING_STATUSES,
    AUCTION_SETTLING_STATUSES,
    Auction,
)
from src.shared.models.bid import BID_ACTIVE, BID_OUTBID, Bid
from src.shared.models.event import EVENT_ARCHIVED, EVENT_PUBLISHED, Event
from src.shared.models.order import Order
from src.shared.models.payout import PAYOUT_RELEASED, Payout
from src.shared.models.piece import Piece
from src.shared.models.social import Follow
from src.shared.models.ticketing import (
    DEBT_OPEN,
    EVENT_PAYOUT_NOTHING,
    EVENT_PAYOUT_RELEASED,
    LIVE_TICKET_STATUSES,
    EventPayout,
    HostFeeDebt,
    Ticket,
)
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

# Orders whose piece is still on its way (or whose money is still held).
SHIPPING_STATUSES = ("pending_payment", "paid", "shipped", "awaiting_confirmation", "disputed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- going public ---------------------------------------------------------------------------------

def approve_waiting_followers(db: Session, user: User) -> int:
    """Turning off Private account: everyone waiting is accepted and told (spec
    `SettingsFollowRequests`). Caller has committed the visibility change. Returns how many."""
    from src.modules.notifications import notifications_dao

    pending = list(db.execute(
        select(Follow).where(Follow.following_id == user.id, Follow.status == "pending")
        .with_for_update()
    ).scalars())
    for follow in pending:
        follow.status = "accepted"
    db.commit()
    for follow in pending:
        try:
            notifications_dao.create_and_push(
                db, user_id=follow.follower_id, type="follow", actor_id=user.id,
                target_type="user", target_id=user.id, payload={"accepted": True},
                title="Follow request accepted", body="Your follow request was accepted",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Follow-accepted notification failed for %s", follow.follower_id)
    return len(pending)


# --- turning off selling ---------------------------------------------------------------------------

def _auctions_with_bids(db: Session, seller_id: uuid.UUID) -> list[Auction]:
    return list(db.execute(
        select(Auction).join(Piece, Piece.id == Auction.piece_id).where(
            Piece.user_id == seller_id,
            Auction.status.in_(AUCTION_RUNNING_STATUSES + AUCTION_SETTLING_STATUSES),
            select(func.count(Bid.id)).where(
                Bid.auction_id == Auction.id, Bid.status.in_((BID_ACTIVE, BID_OUTBID))
            ).scalar_subquery() > 0,
        )
    ).scalars())


def _count_orders(db: Session, column, user_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(Order.id)).where(column == user_id, Order.status.in_(SHIPPING_STATUSES))
    ).scalar_one()


def selling_off_blockers(db: Session, user: User) -> list[str]:
    reasons = []
    if _auctions_with_bids(db, user.id):
        reasons.append("An auction of yours has bids. Wait for it to close, or cancel it first.")
    if _count_orders(db, Order.seller_id, user.id):
        reasons.append("A sale of yours is still going through. Finish it first.")
    return reasons


# --- deleting -----------------------------------------------------------------------------------

def deletion_blockers(db: Session, user: User) -> list[dict]:
    """Everything that has to finish before this account can be deleted. Empty means go."""
    blockers: list[dict] = []

    def add(kind: str, count: int, message: str) -> None:
        if count:
            blockers.append({"kind": kind, "count": int(count), "message": message})

    add("running_auction", len(_auctions_with_bids(db, user.id)),
        "An auction of yours has bids. Wait for it to close.")
    winning = db.execute(
        select(func.count(Auction.id)).join(Bid, Bid.auction_id == Auction.id).where(
            Bid.bidder_id == user.id, Bid.status == BID_ACTIVE,
            Auction.status.in_(AUCTION_RUNNING_STATUSES + AUCTION_SETTLING_STATUSES),
        )
    ).scalar_one()
    add("bidding", winning, "You're bidding on an auction that hasn't finished.")
    add("shipping_sale", _count_orders(db, Order.seller_id, user.id),
        "A piece you sold is still on its way.")
    add("shipping_purchase", _count_orders(db, Order.buyer_id, user.id),
        "A piece you bought is still on its way.")

    art_payouts = db.execute(
        select(func.count(Payout.id)).join(Order, Order.id == Payout.order_id).where(
            Payout.seller_id == user.id, Payout.status != PAYOUT_RELEASED,
            Order.status.in_(("completed",)),
        )
    ).scalar_one()
    event_payouts = db.execute(
        select(func.count(EventPayout.id)).where(
            EventPayout.host_id == user.id,
            EventPayout.status.notin_((EVENT_PAYOUT_RELEASED, EVENT_PAYOUT_NOTHING)),
        )
    ).scalar_one()
    # A finished event whose host payout hasn't been made yet (the day's wait).
    unpaid_events = db.execute(
        select(func.count(Event.id)).outerjoin(EventPayout, EventPayout.event_id == Event.id).where(
            Event.host_id == user.id, Event.status == EVENT_ARCHIVED, EventPayout.id.is_(None),
            select(func.count(Ticket.id)).where(Ticket.event_id == Event.id).scalar_subquery() > 0,
        )
    ).scalar_one()
    add("pending_payout", art_payouts + event_payouts + unpaid_events,
        "Money is still on its way to you.")

    upcoming = db.execute(
        select(func.count(func.distinct(Event.id))).join(Ticket, Ticket.event_id == Event.id).where(
            Event.host_id == user.id, Event.status == EVENT_PUBLISHED,
            Ticket.status.in_(LIVE_TICKET_STATUSES),
        )
    ).scalar_one()
    add("hosted_event_tickets", upcoming,
        "You're hosting an event with tickets sold. Cancel it first (everyone is refunded).")

    debts = db.execute(
        select(func.coalesce(func.sum(HostFeeDebt.amount_cents - HostFeeDebt.recovered_cents), 0))
        .where(HostFeeDebt.host_id == user.id, HostFeeDebt.status == DEBT_OPEN)
    ).scalar_one()
    if debts:
        blockers.append({"kind": "fee_debt", "count": 1, "amountCents": int(debts),
                         "message": "You owe card fees from an event you cancelled."})
    return blockers


def require_deletable(db: Session, user: User) -> None:
    blockers = deletion_blockers(db, user)
    if blockers:
        raise AppError(blockers[0]["message"] + " (See what's left in Delete account.)", 409)


def event_has_tickets(db: Session, event_id: uuid.UUID) -> bool:
    from src.shared.models.ticketing import TicketOrder

    return db.execute(select(TicketOrder.id).where(TicketOrder.event_id == event_id).limit(1)).first() is not None
