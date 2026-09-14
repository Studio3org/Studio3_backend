"""Bids controller — placing a bid on an auction piece."""
import uuid

from flask import request, g

from src.shared.config.database import SessionLocal
from src.shared.utils.app_error import AppError
from src.modules.pieces.pieces_dao import get_piece
from src.modules.user.user_dao import get_user_by_id
from src.modules.bids import bid_dao
from src.modules.notifications import notifications_dao


def _amount_cents(body: dict) -> int:
    raw = body.get("amountCents")
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        raise AppError("amountCents is required.", 400)
    if amount <= 0:
        raise AppError("amountCents must be positive.", 400)
    return amount


def place_bid(piece_id: str):
    body = request.get_json() or {}
    amount_cents = _amount_cents(body)

    db = SessionLocal()
    try:
        bidder = get_user_by_id(db, uuid.UUID(g.user["id"]))
        pid = uuid.UUID(piece_id)
        piece = get_piece(db, pid)
        if not piece:
            raise AppError("Piece not found.", 404)
        if piece.user_id == bidder.id:
            raise AppError("Cannot bid on your own piece.", 400)

        previous_highest = bid_dao.get_highest_bid(db, pid)
        bid = bid_dao.place_bid(db, pid, bidder.id, amount_cents)
        db.refresh(piece)

        try:
            if piece.user_id != bidder.id:
                notifications_dao.create_and_push(
                    db,
                    user_id=piece.user_id,
                    type="bid_placed",
                    actor_id=bidder.id,
                    target_type="piece",
                    target_id=piece.id,
                    payload={"amountCents": amount_cents},
                    title="New bid",
                    body=f"{bidder.name} bid ${amount_cents / 100:.2f} on {piece.title}",
                )
            if previous_highest and previous_highest.bidder_id != bidder.id:
                notifications_dao.create_and_push(
                    db,
                    user_id=previous_highest.bidder_id,
                    type="outbid",
                    actor_id=bidder.id,
                    target_type="piece",
                    target_id=piece.id,
                    payload={"amountCents": amount_cents},
                    title="You've been outbid",
                    body=f"Someone bid higher on {piece.title}",
                )
        except Exception:
            pass

        summary = bid_dao.bid_summary(db, piece)
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
