"""Closing auctions — the sweep that turns held money into a sale, or gives it all back.

Runs on the Celery worker every minute. Failures here are logged, never raised: there is no
caller to return an error to, and one bad auction must not stop the rest of the batch.

Each auction is re-read and re-validated under a row lock before anything is done to it. The
query that selected it is a hint, not a fact — by the time the sweep gets there a bid may
have landed and pushed the close out, or a seller may have cancelled.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.models.auction import (
    AUCTION_CLOSED_NO_BIDS,
    AUCTION_CLOSED_SOLD,
    AUCTION_CLOSING,
    AUCTION_LIVE,
    AUCTION_NEEDS_SELLER_ACTION,
    Auction,
)
from src.shared.models.bid import BID_LOST, BID_WON
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.utils.logger import get_logger
from src.modules.bids import bid_dao, holds_service
from src.modules.notifications import notifications_dao
from src.modules.pieces import piece_state

logger = get_logger(__name__)


def close_expired_auctions() -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        candidates = db.execute(
            select(Auction.id).where(
                Auction.status.in_((AUCTION_LIVE, AUCTION_CLOSING)),
                Auction.closes_at.is_not(None),
                Auction.closes_at <= now,
            )
        ).scalars().all()

        for auction_id in candidates:
            try:
                _close_one(db, auction_id)
            except Exception:
                db.rollback()
                logger.exception("Failed to close auction %s", auction_id)
    finally:
        db.close()


def _close_one(db, auction_id: uuid.UUID) -> None:
    auction = db.execute(
        select(Auction).where(Auction.id == auction_id).with_for_update()
    ).scalar_one_or_none()
    if not auction or auction.status not in (AUCTION_LIVE, AUCTION_CLOSING):
        return
    # Re-checked under the lock: a bid landing in the soft-close window moves this, and the
    # sweep must not close an auction that has just been extended out from under it.
    if not auction.closes_at or auction.closes_at > datetime.now(timezone.utc):
        return

    piece = db.get(Piece, auction.piece_id)
    seller = db.get(User, auction.seller_id)
    winning = bid_dao.get_highest_bid(db, auction.id)

    reserve_met = (
        auction.reserve_cents is None
        or (winning is not None and winning.amount_cents >= auction.reserve_cents)
    )

    if winning is None:
        _close_without_sale(db, auction, piece, AUCTION_CLOSED_NO_BIDS, "auction_no_bids")
        _notify_seller_no_sale(db, seller, piece, "auction_ended_no_bids", "Auction ended",
                               f'"{piece.title}" ended with no bids.' if piece else "Auction ended.")
        return

    if not reserve_met:
        # The client chose option (c): park it and let the seller decide. Nothing is charged,
        # and every bidder gets their money back.
        _close_without_sale(
            db, auction, piece, AUCTION_NEEDS_SELLER_ACTION, "auction_reserve_not_met"
        )
        _notify_seller_no_sale(
            db, seller, piece, "auction_reserve_not_met", "Reserve not met",
            f'"{piece.title}" ended below your reserve. Nothing was charged — decide what to '
            "do next." if piece else "Your auction ended below its reserve.",
        )
        return

    _close_with_winner(db, auction, piece, winning)


def _close_without_sale(db, auction, piece, status: str, reason: str) -> None:
    """No sale: void every bid, release every hold, and park the auction."""
    cancelled = bid_dao.cancel_active_bids(db, auction.id, reason=reason, commit=False)
    auction.status = status
    auction.closed_at = datetime.now(timezone.utc)
    if piece is not None:
        piece_state.transition_piece(
            db, piece, piece_state.DELISTED, allowed_from={piece_state.LIVE},
            reason=reason, commit=False,
        )
    db.commit()

    # After the commit — a failed push must not undo a close.
    for bid in cancelled:
        try:
            notifications_dao.create_and_push(
                db,
                user_id=bid.bidder_id,
                type="auction_no_sale",
                target_type="piece",
                target_id=auction.piece_id,
                title="Auction ended without a sale",
                body="Nothing was charged to you, and your hold has been released.",
            )
        except Exception:
            logger.exception("No-sale notification failed for bidder %s", bid.bidder_id)


def _close_with_winner(db, auction, piece, winning) -> None:
    """Capture the winner and release everyone else.

    Capture first. If the card declines we must not have already released the losing bids —
    the cascade needs them, and a released hold cannot be un-released.
    """
    winning_hold = bid_dao.hold_for_bid(db, winning.id)
    if winning_hold is None:
        logger.error("Winning bid %s has no hold; parking auction %s", winning.id, auction.id)
        auction.status = AUCTION_NEEDS_SELLER_ACTION
        db.commit()
        return

    try:
        holds_service.capture_hold(db, winning_hold, commit=False)
    except Exception as exc:
        # Phase 3 turns this into the retry-and-cascade flow. Until then the auction is
        # parked rather than silently closed, and nobody's money is released: the losing
        # holds are what the cascade will need.
        logger.warning("Capture failed closing auction %s: %s", auction.id, exc)
        auction.status = AUCTION_NEEDS_SELLER_ACTION
        auction.closed_at = datetime.now(timezone.utc)
        db.commit()
        return

    winning.status = BID_WON
    losing = [b for b in bid_dao.ranked_active_bids(db, auction.id) if b.id != winning.id]
    for bid in losing:
        bid.status = BID_LOST
        hold = bid_dao.hold_for_bid(db, bid.id)
        if hold:
            holds_service.release_hold(db, hold, "auction_lost", commit=False)

    auction.status = AUCTION_CLOSED_SOLD
    auction.closed_at = datetime.now(timezone.utc)
    if piece is not None:
        piece_state.transition_piece(
            db, piece, piece_state.AUCTION_WON, allowed_from={piece_state.LIVE},
            reason="auction_closed_with_winner", commit=False,
        )
    db.commit()

    _notify_close(db, auction, piece, winning, losing)


def _notify_close(db, auction, piece, winning, losing) -> None:
    title = piece.title if piece else "a piece"
    try:
        notifications_dao.create_and_push(
            db,
            user_id=winning.bidder_id,
            type="auction_won",
            target_type="piece",
            target_id=auction.piece_id,
            payload={"amountCents": winning.amount_cents},
            title="You won the auction",
            body=f'You won "{title}" for ${winning.amount_cents / 100:.2f}. '
                 "Confirm your delivery details to finish.",
        )
        notifications_dao.create_and_push(
            db,
            user_id=auction.seller_id,
            type="auction_sold",
            actor_id=winning.bidder_id,
            target_type="piece",
            target_id=auction.piece_id,
            payload={"amountCents": winning.amount_cents},
            title="Your auction sold",
            body=f'"{title}" sold for ${winning.amount_cents / 100:.2f}.',
        )
    except Exception:
        logger.exception("Auction-won notifications failed for auction %s", auction.id)

    for bid in losing:
        try:
            notifications_dao.create_and_push(
                db,
                user_id=bid.bidder_id,
                type="auction_lost",
                target_type="piece",
                target_id=auction.piece_id,
                title="You didn't win this auction",
                body=f'"{title}" went to another bidder. Nothing was charged to you — '
                     "see more from this artist.",
            )
        except Exception:
            logger.exception("Auction-lost notification failed for bidder %s", bid.bidder_id)


def _notify_seller_no_sale(db, seller, piece, kind: str, title: str, body: str) -> None:
    if not seller:
        return
    try:
        notifications_dao.create_and_push(
            db, user_id=seller.id, type=kind, target_type="piece",
            target_id=piece.id if piece else None, title=title, body=body,
        )
    except Exception:
        logger.exception("No-sale seller notification failed")
