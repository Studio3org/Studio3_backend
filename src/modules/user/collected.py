"""A profile's Collection: the pieces someone has bought on Studio 3 (BACKEND_CHANGES B37).

`GET /api/users/<username>/collection` lists the pieces in their paid, shipped, awaiting
confirmation or completed orders (the same orders the profile's `collectedCount` counts),
newest purchase first, each once, with its artist. The same rules as their Pieces: a private
account only for the owner and their followers (403 otherwise), and nobody either side of a
block (404). Read-only: nothing about orders, payments or shipping changes.
"""
import uuid
from typing import Iterable

from flask import g, request
from sqlalchemy import func, select

from src.modules.auth.auth_dao import find_user_by_username
from src.modules.orders.orders_dao import COMPLETED_STATUSES
from src.modules.pieces.pieces_dao import batch_list_piece_media, piece_to_dict
from src.modules.social import block_dao, social_dao
from src.modules.user import profile_extras
from src.shared.config.database import SessionLocal
from src.shared.models.order import Order, OrderItem
from src.shared.models.piece import Piece
from src.shared.models.social import CollectionHidden
from src.shared.models.user import User
from src.shared.utils.app_error import AppError

# A profile tab, not an archive: the newest this many purchases.
COLLECTION_LIMIT = 200

# What a Collection tile never shows (`HandoffProfiles`: "No prices").
_PRICE_KEYS = ("price", "priceCents", "currency")


def first_of_each(pieces: Iterable) -> list:
    """Each piece once, keeping the first (newest) time it was bought."""
    seen, out = set(), []
    for piece in pieces:
        if piece.id in seen:
            continue
        seen.add(piece.id)
        out.append(piece)
    return out


def collected_pieces(db, buyer_id: uuid.UUID, limit: int = COLLECTION_LIMIT, *,
                     include_hidden: bool = False) -> list:
    stmt = (
        select(Piece)
        .join(OrderItem, OrderItem.piece_id == Piece.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.buyer_id == buyer_id,
            Order.status.in_(COMPLETED_STATUSES),
            Piece.deleted_at.is_(None),
        )
    )
    if not include_hidden:
        # B32: "Hide from my Collection" takes it off the tab for everyone, the owner too.
        stmt = stmt.where(~select(CollectionHidden.piece_id).where(
            CollectionHidden.user_id == buyer_id, CollectionHidden.piece_id == Piece.id
        ).exists())
    rows = db.execute(stmt.order_by(Order.created_at.desc()).limit(limit)).scalars()
    return first_of_each(rows)


def hidden_ids(db, user_id: uuid.UUID) -> set:
    return set(db.execute(
        select(CollectionHidden.piece_id).where(CollectionHidden.user_id == user_id)
    ).scalars())


def set_hidden(piece_id: str, hidden: bool):
    """`POST /api/collection/<pieceId>/hide` and its undo (`DELETE`): only for a piece the
    caller actually collected."""
    try:
        pid = uuid.UUID(piece_id)
    except ValueError:
        raise AppError("Invalid piece id.", 400) from None
    user_id = uuid.UUID(g.user["id"])
    db = SessionLocal()
    try:
        if pid not in {p.id for p in collected_pieces(db, user_id, include_hidden=True)}:
            raise AppError("That piece isn't in your Collection.", 404)
        row = db.get(CollectionHidden, (user_id, pid))
        if hidden and row is None:
            db.add(CollectionHidden(user_id=user_id, piece_id=pid))
        elif not hidden and row is not None:
            db.delete(row)
        db.commit()
        return {"pieceId": str(pid), "hidden": hidden}, 200
    finally:
        db.close()


def count_artists(db, buyer_id: uuid.UUID) -> int:
    """How many artists they've bought from: the "artists" stat (`ProfileCollector`)."""
    return db.execute(
        select(func.count(func.distinct(Order.seller_id))).where(
            Order.buyer_id == buyer_id, Order.status.in_(COMPLETED_STATUSES)
        )
    ).scalar_one()


def to_item(piece, media, author) -> dict:
    """A piece as a Collection tile: the piece, its artist, no price."""
    data = piece_to_dict(piece, media=media)
    for key in _PRICE_KEYS:
        data.pop(key, None)
    data["author"] = {
        "username": author.username if author else None,
        "name": author.name if author else None,
        "profilePhotoUrl": author.image if author else None,
    }
    return data


def list_for_user(username: str):
    db = SessionLocal()
    try:
        user = find_user_by_username(db, username.lower())
        if not user:
            raise AppError("User not found.", 404)
        viewer_id = uuid.UUID(g.user["id"]) if getattr(g, "user", None) else None
        if viewer_id and block_dao.is_blocked_either_way(db, viewer_id, user.id):
            raise AppError("User not found.", 404)
        if not social_dao.can_view_content(db, user, viewer_id):
            raise AppError("This account is private.", 403)
        # Private Collection (B34): the owner's alone; everyone else sees an empty tab.
        if not profile_extras.collection_visible(user, viewer_id):
            return [], 200

        # The owner can ask for hidden pieces too (?includeHidden=1), to bring one back.
        is_owner = viewer_id == user.id
        include_hidden = is_owner and request.args.get("includeHidden") in ("1", "true")
        pieces = collected_pieces(db, user.id, include_hidden=include_hidden)
        if not pieces:
            return [], 200
        media = batch_list_piece_media(db, [p.id for p in pieces])
        artist_ids = {p.user_id for p in pieces}
        artists = {u.id: u for u in db.execute(select(User).where(User.id.in_(artist_ids))).scalars()}
        hidden = hidden_ids(db, user.id) if is_owner else set()
        items = [to_item(p, media.get(p.id, []), artists.get(p.user_id)) for p in pieces]
        if is_owner:
            for item, piece in zip(items, pieces):
                item["hidden"] = piece.id in hidden
        return items, 200
    finally:
        db.close()
