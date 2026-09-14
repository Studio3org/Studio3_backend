"""Closes expired auctions — the one scheduled job in this codebase (see
src/shared/scheduler.py). Runs on an interval rather than being triggered by any request,
so failures here are logged, never raised to a caller."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.utils.logger import get_logger
from src.modules.bids import bid_dao
from src.modules.notifications import notifications_dao

logger = get_logger(__name__)


def close_expired_auctions() -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        candidate_ids = db.execute(
            select(Piece.id).where(
                Piece.listing_type == "auction",
                Piece.status == "live",
                Piece.auction_ends_at.is_not(None),
                Piece.auction_ends_at <= now,
            )
        ).scalars().all()

        for piece_id in candidate_ids:
            try:
                _close_one(db, piece_id)
            except Exception:
                db.rollback()
                logger.exception("Failed to close auction for piece %s", piece_id)
    finally:
        db.close()


def _close_one(db, piece_id: uuid.UUID) -> None:
    piece = db.execute(
        select(Piece).where(Piece.id == piece_id).with_for_update()
    ).scalar_one_or_none()
    if not piece or piece.status != "live" or piece.listing_type != "auction":
        return
    if not piece.auction_ends_at or piece.auction_ends_at > datetime.now(timezone.utc):
        return

    highest = bid_dao.get_highest_bid(db, piece.id)
    owner = db.get(User, piece.user_id)

    if highest:
        piece.status = "auction_won"
        db.commit()
        try:
            notifications_dao.create_and_push(
                db,
                user_id=highest.bidder_id,
                type="auction_won",
                target_type="piece",
                target_id=piece.id,
                payload={"amountCents": highest.amount_cents},
                title="You won the auction",
                body=f"You won \"{piece.title}\" for ${highest.amount_cents / 100:.2f} — complete your purchase.",
            )
            if owner:
                notifications_dao.create_and_push(
                    db,
                    user_id=owner.id,
                    type="auction_sold",
                    actor_id=highest.bidder_id,
                    target_type="piece",
                    target_id=piece.id,
                    payload={"amountCents": highest.amount_cents},
                    title="Your auction sold",
                    body=f"\"{piece.title}\" sold for ${highest.amount_cents / 100:.2f}, pending the buyer's checkout.",
                )
        except Exception:
            logger.exception("Auction-won notifications failed for piece %s", piece.id)
    else:
        piece.status = "delisted"
        db.commit()
        if owner:
            try:
                notifications_dao.create_and_push(
                    db,
                    user_id=owner.id,
                    type="auction_ended_no_bids",
                    target_type="piece",
                    target_id=piece.id,
                    title="Auction ended",
                    body=f"\"{piece.title}\" ended with no bids.",
                )
            except Exception:
                logger.exception("Auction-ended notification failed for piece %s", piece.id)
