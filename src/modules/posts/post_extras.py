"""What a Scene payload carries beyond its own row (BACKEND_CHANGES B33).

`saveCount` beside `isSaved` (the viewer shows Save with its count), and `linkedPieces`: the
pieces the Scene shows on, each with its title, picture, artist and price, so the viewer
doesn't fetch them one by one. Batched for a page of Scenes.
"""
import uuid
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.models.piece import Piece
from src.shared.models.post import Post
from src.shared.models.user import User
from src.modules.pieces.pieces_dao import piece_listing_state
from src.modules.social import social_dao


def linked_ids(post: Post) -> list[uuid.UUID]:
    """Every piece the Scene links to, in order (the older single link when that's all)."""
    if post.linked_piece_ids:
        return list(post.linked_piece_ids)
    return [post.linked_piece_id] if post.linked_piece_id else []


def linked_piece_dict(piece: Piece, author: Optional[User]) -> dict:
    """The few fields the viewer's linked-piece line and its sheet draw."""
    return {
        "id": str(piece.id),
        "userId": str(piece.user_id),
        "title": piece.title,
        "mediaUrl": piece.media_url,
        "mediaType": piece.media_type,
        "mediaAspectRatio": piece.media_aspect_ratio,
        "isForSale": piece.is_for_sale,
        "listingType": piece.listing_type,
        "listingState": piece_listing_state(piece),
        "priceCents": piece.price_cents,
        "currency": piece.currency,
        "status": piece.status,
        "author": (
            {"username": author.username, "name": author.name, "profilePhotoUrl": author.image}
            if author
            else None
        ),
    }


def linked_pieces(
    db: Session, posts: Iterable[Post], viewer_id: Optional[uuid.UUID]
) -> dict[uuid.UUID, list[dict]]:
    """Each Scene's linked pieces. A deleted piece is left out, and so is a draft piece for
    anyone but its artist."""
    posts = list(posts)
    wanted = {pid for post in posts for pid in linked_ids(post)}
    if not wanted:
        return {post.id: [] for post in posts}
    pieces = {
        p.id: p
        for p in db.execute(
            select(Piece).where(Piece.id.in_(wanted), Piece.deleted_at.is_(None))
        ).scalars()
    }
    author_ids = {p.user_id for p in pieces.values()}
    authors = (
        {u.id: u for u in db.execute(select(User).where(User.id.in_(author_ids))).scalars()}
        if author_ids
        else {}
    )
    pairs = []
    result: dict[uuid.UUID, list[dict]] = {}
    for post in posts:
        items = []
        for pid in linked_ids(post):
            piece = pieces.get(pid)
            if piece is None or piece.status == "deleted":
                continue
            if piece.status == "draft" and piece.user_id != viewer_id:
                continue
            d = linked_piece_dict(piece, authors.get(piece.user_id))
            pairs.append((piece, d))
            items.append(d)
        result[post.id] = items
    if pairs:
        # An auction piece's line shows its current bid (BACKEND_CHANGES B21).
        from src.modules.bids import auction_list_fields

        auction_list_fields.add_to(db, pairs)
    return result


def add_to(db: Session, pairs: list[tuple[Post, dict]], viewer_id: Optional[uuid.UUID]) -> None:
    """Merge `saveCount` and `linkedPieces` into each Scene's dict, in place."""
    if not pairs:
        return
    posts = [post for post, _ in pairs]
    saves = social_dao.batch_save_counts(db, "post", [p.id for p in posts])
    linked = linked_pieces(db, posts, viewer_id)
    for post, d in pairs:
        d["saveCount"] = saves.get(post.id, 0)
        d["linkedPieces"] = linked.get(post.id, [])
