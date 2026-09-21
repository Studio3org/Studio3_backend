"""Bulk listing operations that span pieces, bids and notifications.

Lives here rather than in user_dao because withdrawing a seller's work is a listing concern,
not a user-record one, and user_dao has no business importing bids or notifications.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.models.piece import Piece
from src.shared.utils.logger import get_logger
from src.modules.bids import auction_cancellation, bid_dao
from src.modules.pieces import piece_state

logger = get_logger(__name__)


def delist_all_for_seller(db: Session, user_id: uuid.UUID, reason: str) -> dict:
    """Withdraw every for-sale piece belonging to a seller.

    Live auctions go through cancel_auction so their bidders get their holds released and
    are told the seller withdrew. The previous implementation set is_for_sale=False and
    status='delisted' directly, which took the piece out of the close sweep's query — so the
    auction never closed, and nobody bidding on it was ever notified of anything.

    Pieces with a sale in flight (reserved/sold/auction_won) are left alone: there is an
    order pointing at them and they are not the seller's to withdraw any more.
    """
    pieces = list(
        db.execute(
            select(Piece).where(Piece.user_id == user_id, Piece.is_for_sale.is_(True))
        ).scalars()
    )

    cancelled_auctions = 0
    delisted = 0
    skipped = 0
    notifications: list[tuple[Piece, list]] = []

    for piece in pieces:
        if piece.status in piece_state.COMMITTED_STATUSES:
            skipped += 1
            continue
        if piece.listing_type == "auction" and piece.status == piece_state.LIVE:
            bids = auction_cancellation.cancel_auction(db, piece, reason=reason, commit=False)
            cancelled_auctions += 1
            if bids:
                notifications.append((piece, bids))
            continue
        if piece.status == piece_state.LIVE:
            piece_state.transition_piece(
                db, piece, piece_state.DELISTED, reason=reason, commit=False
            )
        piece.is_for_sale = False
        delisted += 1

    db.commit()

    # After the commit, so a failed push can't undo the withdrawal.
    for piece, bids in notifications:
        auction_cancellation.notify_cancelled_bidders(db, piece, bids, reason)

    logger.info(
        "Delisted %d and cancelled %d auctions for seller %s (%d left alone, sale in flight).",
        delisted, cancelled_auctions, user_id, skipped,
    )
    return {
        "delisted": delisted,
        "cancelledAuctions": cancelled_auctions,
        "skipped": skipped,
    }


def active_bid_count(db: Session, piece: Piece) -> int:
    """Convenience for callers that need to word a confirmation before cancelling."""
    return bid_dao.count_active_bids_for_piece(db, piece.id)
