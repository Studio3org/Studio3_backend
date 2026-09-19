"""Bids controller — placing a bid on an auction piece."""
import uuid

from flask import g, request

from src.shared.config.database import SessionLocal
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.bids import auction_dao, bid_dao
from src.modules.notifications import notifications_dao
from src.modules.pieces.pieces_dao import get_piece
from src.modules.user.user_dao import get_user_by_id

logger = get_logger(__name__)


def _amount_cents(body: dict) -> int:
    raw = body.get("amountCents")
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        raise AppError("amountCents is required.", 400) from None
    if amount <= 0:
        raise AppError("amountCents must be positive.", 400)
    return amount


def place_bid(piece_id: str):
    """Place a bid, authorising the money behind it.

    A payment method is required on a bidder's first bid and optional afterwards — raising
    your own bid reuses the card already committed to this auction rather than asking again.
    """
    body = request.get_json() or {}
    amount_cents = _amount_cents(body)
    payment_method_id = (body.get("paymentMethodId") or "").strip() or None

    db = SessionLocal()
    try:
        bidder = get_user_by_id(db, uuid.UUID(g.user["id"]))
        piece = get_piece(db, uuid.UUID(piece_id))
        if not piece:
            raise AppError("Piece not found.", 404)

        auction = auction_dao.get_running_auction(db, piece.id)
        if auction is None:
            raise AppError("This piece is not up for auction.", 400)

        # Captured before the bid lands, so we know who to tell they have been overtaken.
        previous_highest = bid_dao.get_highest_bid(db, auction.id)

        bid = bid_dao.place_bid(db, auction.id, bidder, amount_cents, payment_method_id)

        db.refresh(auction)
        summary = bid_dao.bid_summary(db, auction, viewer_id=bidder.id)

        _notify(db, auction, piece, bidder, bid, previous_highest)

        return {
            "id": str(bid.id),
            "pieceId": str(piece.id),
            "amountCents": bid.amount_cents,
            "bidderId": str(bid.bidder_id),
            "createdAt": bid.created_at.isoformat(),
            **summary,
        }, 201
    finally:
        db.close()


def _notify(db, auction, piece, bidder, bid, previous_highest) -> None:
    """Tell the bidder they're winning, and whoever they passed that they aren't.

    Never raises: a failed push must not undo a bid that has already been placed and has real
    money authorised behind it.
    """
    try:
        notifications_dao.create_and_push(
            db,
            user_id=bidder.id,
            type="bid_placed",
            target_type="piece",
            target_id=piece.id,
            payload={"amountCents": bid.amount_cents},
            title="You're currently winning",
            body=f'Your bid of ${bid.amount_cents / 100:.2f} leads on "{piece.title}".',
        )
        if previous_highest and previous_highest.bidder_id != bidder.id:
            notifications_dao.create_and_push(
                db,
                user_id=previous_highest.bidder_id,
                type="outbid",
                actor_id=bidder.id,
                target_type="piece",
                target_id=piece.id,
                payload={"amountCents": bid.amount_cents},
                title="You've been outbid",
                # Deliberately does not name who: bidders never see each other's identities.
                body=f'Someone bid higher on "{piece.title}". Your hold still stands — '
                     "raise your bid to get back in front.",
            )
    except Exception:
        logger.exception("Bid notifications failed for bid %s", bid.id)
