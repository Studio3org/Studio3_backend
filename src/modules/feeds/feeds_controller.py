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
from src.modules.feeds import feed_extras
from src.modules.pieces.pieces_dao import batch_list_piece_media, piece_to_dict
from src.modules.posts.posts_dao import post_to_dict
from src.modules.user.user_dao import get_user_by_id
from src.modules.social import social_dao, block_dao

from src.modules.pieces import piece_state

DEFAULT_LIMIT = 20
MAX_LIMIT = 50

# Live work plus sold/reserved/auction_won so home can show Available vs Collected.
# Read from piece_state rather than restated here: the two lists drifted, and `auction_won`
# being absent meant a piece silently vanished from every feed the moment its auction closed.
_FEED_PIECE_STATUSES = piece_state.PUBLIC_STATUSES


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
    # Every photo of each piece, cover first, and the series it is in (BACKEND_CHANGES B29).
    media = batch_list_piece_media(db, piece_ids)
    series = feed_extras.series_for_pieces(db, piece_ids)

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
    piece_dicts = []
    for p in pieces:
        d = piece_to_dict(p, media=media.get(p.id, []))
        piece_dicts.append((p, d))
        d["type"] = "piece"
        if p.id in series:
            d["series"] = series[p.id]
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
    # Auction pieces carry their bid, bid count and end time (BACKEND_CHANGES B21).
    from src.modules.bids import auction_list_fields

    auction_list_fields.add_to(db, piece_dicts)

    items.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return items


def _wants_events() -> bool:
    """`?include=events`: the app can show an event item (B27). Asked for, because an older app
    reads any item it doesn't know as a piece and would draw an empty tile."""
    return "events" in {p.strip() for p in (request.args.get("include") or "").split(",")}


def _add_feed_event(db, result: dict, viewer_id: Optional[uuid.UUID], hidden: set):
    """One `{"type": "event", "event": <card>}` on the first page (B27), after the third item:
    starting in the next two weeks, a followed host's first, then one near the viewer."""
    if request.args.get("cursor") or not _wants_events():
        return
    from src.modules.events import event_discovery
    from src.modules.events.events_controller import event_card

    at = event_discovery.feed_slot(len(result["items"]))
    if at < 0:
        return
    event = event_discovery.feed_event(db, viewer_id, hidden=hidden)
    if event is not None:
        result["items"].insert(at, {"type": "event", "event": event_card(db, event, viewer_id)})


def _upcoming_shows(db, viewer_id: Optional[uuid.UUID], hidden: set) -> list:
    """Explore's Upcoming shows (B27): event cards, soonest first, each with `pieces` from its
    bill."""
    from src.modules.events import event_discovery
    from src.modules.events.events_controller import event_card

    return [
        {**event_card(db, event, viewer_id), "pieces": pieces}
        for event, pieces in event_discovery.upcoming_shows(db, hidden=hidden)
    ]


def _paginated_response(db, piece_query, post_query, viewer_id: Optional[uuid.UUID]):
    hidden = block_dao.blocked_user_ids(db, viewer_id)
    if hidden:
        if piece_query is not None:
            piece_query = piece_query.where(Piece.user_id.notin_(hidden))
        if post_query is not None:
            post_query = post_query.where(Post.user_id.notin_(hidden))
    limit = min(int(request.args.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
    cursor = request.args.get("cursor")
    cursor_tuple = _decode_cursor(cursor) if cursor else None
    pieces, posts = _fetch_page(db, piece_query, post_query, limit + 1, cursor_tuple)
    merged = _enrich_items(db, pieces, posts, viewer_id)
    page = merged[:limit]
    next_cursor = _encode_cursor(page[-1][0], page[-1][1]) if len(merged) > limit and page else None
    result = {"items": [d for _, _, d in page], "nextCursor": next_cursor}
    _add_feed_event(db, result, viewer_id, hidden)
    return result


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


def explore_feed(shows: bool = True):
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

        result = _paginated_response(db, piece_query, post_query, viewer_id)
        # Explore's Upcoming shows ride on the first page of the whole grid (B27).
        if shows and not medium and not request.args.get("cursor"):
            result["upcomingShows"] = _upcoming_shows(
                db, viewer_id, block_dao.blocked_user_ids(db, viewer_id)
            )
        return result, 200
    finally:
        db.close()


def for_you_feed():
    """For You (BACKEND_CHANGES B29): pieces and Scenes ranked for the viewer — people they
    follow, then their taste (the mediums, styles and themes chosen at onboarding) and people
    whose work they liked or saved, then the rest — newest first within each rank, with a
    cursor that carries the rank. A guest, or someone with nothing to go on, gets newest first.
    `?available=1` keeps only pieces that can be collected now.

    Open to signed-out guests too (App Store guideline 5.1.1(v): browsing isn't an account
    feature)."""
    viewer_id = uuid.UUID(g.user["id"]) if getattr(g, "user", None) else None
    available = feed_extras.wants_available(request.args.get("available"))
    db = SessionLocal()
    try:
        signals = feed_extras.Signals.for_viewer(db, viewer_id)
        piece_rank = feed_extras.piece_rank(signals)
        post_rank = feed_extras.post_rank(signals)
        piece_query = select(Piece, piece_rank.label("rank")).where(
            Piece.deleted_at.is_(None), Piece.status.in_(_FEED_PIECE_STATUSES)
        )
        post_query = None
        if available:
            piece_query = feed_extras.available_only(piece_query)
        else:
            post_query = select(Post, post_rank.label("rank")).where(
                Post.deleted_at.is_(None), Post.status == "live"
            )
        hidden = block_dao.blocked_user_ids(db, viewer_id)
        if hidden:
            piece_query = piece_query.where(Piece.user_id.notin_(hidden))
            if post_query is not None:
                post_query = post_query.where(Post.user_id.notin_(hidden))

        limit = min(int(request.args.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        raw_cursor = request.args.get("cursor")
        cursor = feed_extras.decode_cursor(raw_cursor) if raw_cursor else None
        if cursor:
            piece_query = piece_query.where(
                feed_extras.after_cursor(piece_rank, Piece.created_at, Piece.id, cursor)
            )
            if post_query is not None:
                post_query = post_query.where(
                    feed_extras.after_cursor(post_rank, Post.created_at, Post.id, cursor)
                )

        piece_rows = db.execute(
            piece_query.order_by(piece_rank.asc(), Piece.created_at.desc(), Piece.id.desc()).limit(limit + 1)
        ).all()
        post_rows = (
            db.execute(
                post_query.order_by(post_rank.asc(), Post.created_at.desc(), Post.id.desc()).limit(limit + 1)
            ).all()
            if post_query is not None
            else []
        )
        ranks = {row[0].id: row[1] for row in [*piece_rows, *post_rows]}
        merged = _enrich_items(db, [r[0] for r in piece_rows], [r[0] for r in post_rows], viewer_id)
        ordered = feed_extras.order_ranked([(ranks[i], ts, i, d) for ts, i, d in merged])
        page = ordered[:limit]
        next_cursor = (
            feed_extras.encode_cursor(page[-1][0], page[-1][1], page[-1][2])
            if len(ordered) > limit and page
            else None
        )
        result = {
            "items": [d for _, _, _, d in page],
            "nextCursor": next_cursor,
            # Ranked for the viewer now; kept for apps that read it.
            "stub": False,
        }
        _add_feed_event(db, result, viewer_id, hidden)
        return result, 200
    finally:
        db.close()
