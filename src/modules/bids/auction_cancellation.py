"""Cancelling a live auction — one primitive, several callers.

Withdrawing an auction that people have bid on is never just a status change: every bidder
has money committed to it and has to be told, in wording that reads as the seller's decision
rather than a system failure. Doing that in each caller is how seller deactivation ended up
delisting live auctions and stranding their bidders silently.

Callers today: seller deactivation. Callers shortly: the seller-cancellation endpoint, and
tagging a piece to an event (which ends whatever listing the piece already had). They differ
only in `reason`, authorization and where the piece lands afterwards.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.models.bid import Bid
from src.shared.models.auction import AUCTION_CANCELLED, AUCTION_CLOSING, AUCTION_LIVE, Auction
from src.shared.models.piece import Piece
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.bids import bid_dao
from src.modules.pieces import piece_state

logger = get_logger(__name__)

REASON_SELLER_DEACTIVATED = "seller_deactivated"
REASON_SELLER_CANCELLED = "seller_cancelled"
REASON_EVENT_TAGGED = "event_tagged"
REASON_ADMIN = "admin"


def cancel_auction(
    db: Session,
    piece: Piece,
    reason: str,
    *,
    actor_id: Optional[uuid.UUID] = None,
    target_status: str = piece_state.DELISTED,
    commit: bool = True,
) -> list[Bid]:
    """Void every active bid, release its hold, move the piece, and notify the bidders.

    Idempotent by state: an auction the sweep already finished is left alone and an empty
    list comes back, so racing the close is safe rather than an error.

    Notifications are sent after the commit — a push failure must not undo a cancellation.
    """
    locked = db.execute(
        select(Piece).where(Piece.id == piece.id).with_for_update()
    ).scalar_one_or_none()
    if not locked:
        raise AppError("Piece not found.", 404)
    if locked.listing_type != "auction":
        raise AppError("This piece is not up for auction.", 400)

    auction = db.execute(
        select(Auction)
        .where(Auction.piece_id == locked.id, Auction.status.in_((AUCTION_LIVE, AUCTION_CLOSING)))
        .with_for_update()
    ).scalar_one_or_none()
    if auction is None:
        return []

    # Releasing happens inside cancel_active_bids, so no path can void a bid and forget the
    # money committed to it.
    bids = bid_dao.cancel_active_bids(db, auction.id, reason=reason, commit=False)
    auction.status = AUCTION_CANCELLED
    auction.cancelled_reason = reason[:64]
    auction.closed_at = datetime.now(timezone.utc)

    # Bids are already voided, so transition_piece's "auction has active bids" guard passes.
    # That ordering is deliberate: the guard needs no escape hatch, and any future caller
    # that tries to delist around this function still hits it.
    if locked.status == piece_state.LIVE:
        piece_state.transition_piece(
            db, locked, target_status, allowed_from={piece_state.LIVE},
            reason=reason, commit=False,
        )

    if commit:
        db.commit()
        _record(db, auction, locked, bids, reason, actor_id)
        notify_cancelled_bidders(db, locked, bids, reason)
    return bids


def _record(db, auction, piece, bids, reason: str, actor_id) -> None:
    """Leave a durable trace of a cancellation.

    This releases every bidder's hold, so "who withdrew this and why" is exactly the question
    somebody asks a week later — and application logs are gone by then.

    Attributed to a person when one did it, to the system when a sweep did. A seller
    withdrawing their own auction and a scheduled job doing it are different events.
    """
    from src.modules.admin import audit_service
    from src.shared.models.audit import AUDIT_AUCTION_CANCELLED
    from src.shared.models.user import User

    actor = db.get(User, actor_id) if actor_id else None
    detail = {"reason": reason, "cancelledBids": len(bids), "pieceId": str(piece.id)}
    if actor is None:
        audit_service.system(
            db, AUDIT_AUCTION_CANCELLED,
            subject_type="auction", subject_id=auction.id, detail=detail,
        )
    else:
        audit_service.record(
            db, AUDIT_AUCTION_CANCELLED, actor=actor,
            subject_type="auction", subject_id=auction.id, detail=detail,
        )


def notify_cancelled_bidders(
    db: Session, piece: Piece, bids: list[Bid], reason: str
) -> None:
    """One notification per distinct bidder, worded as a seller decision.

    Call after the caller's commit. Failures are logged, never raised: the cancellation has
    already happened and must not be rolled back because a push failed.
    """
    if not bids:
        return
    from src.modules.notifications import notifications_dao

    highest_per_bidder: dict[uuid.UUID, int] = {}
    for bid in bids:
        current = highest_per_bidder.get(bid.bidder_id, 0)
        highest_per_bidder[bid.bidder_id] = max(current, bid.amount_cents)

    for bidder_id, amount_cents in highest_per_bidder.items():
        try:
            notifications_dao.create_and_push(
                db,
                user_id=bidder_id,
                type="auction_cancelled",
                target_type="piece",
                target_id=piece.id,
                payload={"amountCents": amount_cents, "reason": reason},
                title="Auction cancelled",
                body=(
                    f'The seller cancelled the auction for "{piece.title}". '
                    "Nothing has been charged to you."
                ),
            )
        except Exception:
            logger.exception(
                "Auction-cancelled notification failed for bidder %s on piece %s",
                bidder_id, piece.id,
            )
