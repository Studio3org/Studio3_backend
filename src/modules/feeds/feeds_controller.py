"""Feed endpoints."""
import base64
import uuid
from datetime import datetime
from typing import Optional

from flask import request, g

from sqlalchemy import select, tuple_

from src.shared.config.database import SessionLocal
from src.shared.models.social import Follow
from src.shared.models.piece import Piece
from src.shared.models.post import Post
from src.shared.models.user import User
from src.modules.pieces.pieces_dao import piece_to_dict
from src.modules.posts.posts_dao import post_to_dict
from src.modules.user.user_dao import get_user_by_id
from src.modules.social import social_dao

DEFAULT_LIMIT = 20
MAX_LIMIT = 50

# Live work plus sold/reserved listings so home can show Available vs Collected.
_FEED_PIECE_STATUSES = ("live", "sold", "reserved")


def _encode_cursor(created_at, item_id) -> str:
    raw = f"{created_at.isoformat()}|{item_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str):
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), uuid.UUID(id_str)
    except Exception:
        return None


def _fetch_page(db, piece_query, post_query, limit: int, cursor_tuple):
    if cursor_tuple:
        cursor_ts, cursor_id = cursor_tuple
        if piece_query is not None:
            piece_query = piece_query.where(tuple_(Piece.created_at, Piece.id) < tuple_(cursor_ts, cursor_id))
        if post_query is not None:
            post_query = post_query.where(tuple_(Post.created_at, Post.id) < tuple_(cursor_ts, cursor_id))
    pieces = []
    if piece_query is not None:
        pieces = list(
            db.execute(piece_query.order_by(Piece.created_at.desc(), Piece.id.desc()).limit(limit)).scalars().all()
        )
    posts = []
    if post_query is not None:
        posts = list(
            db.execute(post_query.order_by(Post.created_at.desc(), Post.id.desc()).limit(limit)).scalars().all()
        )
    return pieces, posts


def _enrich_items(db, pieces, posts, viewer_id: Optional[uuid.UUID]):
    piece_ids = [p.id for p in pieces]
    post_ids = [p.id for p in posts]
    piece_like_counts = social_dao.batch_like_counts(db, "piece", piece_ids)
    post_like_counts = social_dao.batch_like_counts(db, "post", post_ids)
    piece_comment_counts = social_dao.batch_comment_counts(db, "piece", piece_ids)
    post_comment_counts = social_dao.batch_comment_counts(db, "post", post_ids)
    liked_pieces = social_dao.batch_user_likes(db, "piece", piece_ids, viewer_id)
    liked_posts = social_dao.batch_user_likes(db, "post", post_ids, viewer_id)
    saved_pieces = social_dao.batch_user_saves(db, "piece", piece_ids, viewer_id)
    saved_posts = social_dao.batch_user_saves(db, "post", post_ids, viewer_id)

    author_ids = {p.user_id for p in pieces} | {p.user_id for p in posts}
    authors = {}
    if author_ids:
        authors = {u.id: u for u in db.execute(select(User).where(User.id.in_(author_ids))).scalars()}
    following_authors = social_dao.batch_accepted_following(db, viewer_id, list(author_ids))

    def _author_block(user_id):
        author = authors.get(user_id)
        if not author:
            return None
        return {
            "username": author.username,
            "name": author.name,
            "profilePhotoUrl": author.image,
            "isFollowing": user_id in following_authors,
        }

    items = []
    for p in pieces:
        d = piece_to_dict(p)
        d["type"] = "piece"
        d["author"] = _author_block(p.user_id)
        d["likeCount"] = piece_like_counts.get(p.id, 0)
        d["commentCount"] = piece_comment_counts.get(p.id, 0)
        d["isLiked"] = p.id in liked_pieces
        d["isSaved"] = p.id in saved_pieces
        items.append((p.created_at, p.id, d))
    for p in posts:
        d = post_to_dict(p)
        d["type"] = "post"
        d["author"] = _author_block(p.user_id)
        d["likeCount"] = post_like_counts.get(p.id, 0)
        d["commentCount"] = post_comment_counts.get(p.id, 0)
        d["isLiked"] = p.id in liked_posts
        d["isSaved"] = p.id in saved_posts
        items.append((p.created_at, p.id, d))

    items.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return items


def _paginated_response(db, piece_query, post_query, viewer_id: Optional[uuid.UUID]):
    limit = min(int(request.args.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
    cursor = request.args.get("cursor")
    cursor_tuple = _decode_cursor(cursor) if cursor else None
    pieces, posts = _fetch_page(db, piece_query, post_query, limit + 1, cursor_tuple)
    merged = _enrich_items(db, pieces, posts, viewer_id)
    page = merged[:limit]
    next_cursor = _encode_cursor(page[-1][0], page[-1][1]) if len(merged) > limit and page else None
    return {"items": [d for _, _, d in page], "nextCursor": next_cursor}


def following_feed():
    db = SessionLocal()
    try:
        me = get_user_by_id(db, uuid.UUID(g.user["id"]))
        following_ids = list(
            db.execute(
                select(Follow.following_id).where(
                    Follow.follower_id == me.id, Follow.status == "accepted"
                )
            ).scalars().all()
        )
        following_ids.append(me.id)
        piece_query = select(Piece).where(
            Piece.user_id.in_(following_ids),
            Piece.deleted_at.is_(None),
            Piece.status.in_(_FEED_PIECE_STATUSES),
        )
        post_query = select(Post).where(
            Post.user_id.in_(following_ids), Post.deleted_at.is_(None), Post.status == "live"
        )
        return _paginated_response(db, piece_query, post_query, me.id), 200
    finally:
        db.close()


def explore_feed():
    medium = request.args.get("medium")
    viewer_id = uuid.UUID(g.user["id"]) if getattr(g, "user", None) else None
    db = SessionLocal()
    try:
        piece_query = select(Piece).where(
            Piece.deleted_at.is_(None),
            Piece.status.in_(_FEED_PIECE_STATUSES),
        )
        post_query = select(Post).where(Post.deleted_at.is_(None), Post.status == "live")

        if medium == "video":
            piece_query = None
            post_query = post_query.where(Post.media_type.in_(("video", "reel", "reels")))
        elif medium:
            post_query = None
            piece_query = piece_query.where(Piece.medium == medium)

        return _paginated_response(db, piece_query, post_query, viewer_id), 200
    finally:
        db.close()


def for_you_feed():
    explore, status = explore_feed()
    explore["stub"] = True
    return explore, status
