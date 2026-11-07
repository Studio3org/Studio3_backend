"""What the home feed adds to its pages (BACKEND_CHANGES B29): every photo of a piece, the
series a piece is in, a For You order for the viewer, and the Available filter.

Each helper works on a whole page at once, so a page costs a fixed number of queries however
many pieces it holds.
"""
import base64
import uuid
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import and_, case, false, func, literal, or_, select, tuple_
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session
from sqlalchemy.types import String

from src.modules.pieces import piece_state
from src.modules.pieces.pieces_dao import piece_listing_state
from src.shared.models.piece import Piece
from src.shared.models.piece_media import PieceMedia
from src.shared.models.post import Post
from src.shared.models.series import Series, SeriesPiece
from src.shared.models.social import Follow, Like, Save
from src.shared.models.user import User

# A series row shows this many of its pieces before See all.
SERIES_PREVIEW = 6

# How much of the viewer's recent likes and saves count as "activity" for For You.
ACTIVITY_LOOKBACK = 200

# For You ranks: someone the viewer follows; their taste or someone whose work they liked or
# saved; everything else. Newest first within each.
RANK_FOLLOWED = 0
RANK_TASTE = 1
RANK_REST = 2


# --- photos and series ---------------------------------------------------------------------------

def media_counts(db: Session, piece_ids: list) -> dict:
    """How many photos each piece has (at least the cover)."""
    if not piece_ids:
        return {}
    rows = db.execute(
        select(PieceMedia.piece_id, func.count(PieceMedia.id))
        .where(PieceMedia.piece_id.in_(piece_ids))
        .group_by(PieceMedia.piece_id)
    ).all()
    return {piece_id: count for piece_id, count in rows}


def series_for_pieces(db: Session, piece_ids: list) -> dict:
    """`{piece_id: series dict}` for the pieces in a series: `id`, `name`, `pieceIds` (its public
    pieces in series order) and up to `SERIES_PREVIEW` `previewPieces` (`id`, `title`,
    `mediaUrl`, `listingState`, `imageCount`). Pieces in no series are left out."""
    if not piece_ids:
        return {}
    links = db.execute(
        select(SeriesPiece.piece_id, SeriesPiece.series_id).where(SeriesPiece.piece_id.in_(piece_ids))
    ).all()
    if not links:
        return {}
    series_ids = {series_id for _, series_id in links}
    series = {s.id: s for s in db.execute(select(Series).where(Series.id.in_(series_ids))).scalars()}
    members = db.execute(
        select(SeriesPiece.series_id, Piece)
        .join(Piece, Piece.id == SeriesPiece.piece_id)
        .where(
            SeriesPiece.series_id.in_(series_ids),
            Piece.deleted_at.is_(None),
            Piece.status.in_(piece_state.PUBLIC_STATUSES),
        )
        .order_by(SeriesPiece.series_id, SeriesPiece.position.asc(), SeriesPiece.created_at.asc())
    ).all()
    by_series: dict = {}
    for series_id, piece in members:
        by_series.setdefault(series_id, []).append(piece)
    previews = [p for pieces in by_series.values() for p in pieces[:SERIES_PREVIEW]]
    counts = media_counts(db, [p.id for p in previews])

    def to_dict(series_id) -> Optional[dict]:
        row = series.get(series_id)
        pieces = by_series.get(series_id) or []
        if row is None or not pieces:
            return None
        return {
            "id": str(row.id),
            "name": row.name,
            "pieceIds": [str(p.id) for p in pieces],
            "previewPieces": [
                {
                    "id": str(p.id),
                    "title": p.title,
                    "mediaUrl": p.media_url,
                    "listingState": piece_listing_state(p),
                    "imageCount": max(counts.get(p.id, 0), 1),
                }
                for p in pieces[:SERIES_PREVIEW]
            ],
        }

    built = {sid: to_dict(sid) for sid in series_ids}
    return {piece_id: built[series_id] for piece_id, series_id in links if built.get(series_id)}


# --- Available -----------------------------------------------------------------------------------

def available_only(piece_query):
    """`?available=1`: pieces that can be collected now, at a price or in a live auction
    (`listingState` `available` or `auction_live`): for sale and live."""
    return piece_query.where(Piece.is_for_sale.is_(True), Piece.status == "live")


def wants_available(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes")


# --- For You -------------------------------------------------------------------------------------

class Signals:
    """What For You knows about the viewer: who they follow, whose work they liked or saved
    lately, and the mediums, styles and themes they chose at onboarding."""

    def __init__(self, followed=(), engaged=(), mediums=(), tags=()):
        self.followed = set(followed)
        self.engaged = set(engaged) - self.followed
        self.mediums = {m.strip().lower() for m in mediums if m and m.strip()}
        self.tags = {t.strip() for t in tags if t and t.strip()}

    @property
    def empty(self) -> bool:
        return not (self.followed or self.engaged or self.mediums or self.tags)

    @classmethod
    def for_viewer(cls, db: Session, viewer_id: Optional[uuid.UUID]) -> "Signals":
        if viewer_id is None:
            return cls()
        followed = db.execute(
            select(Follow.following_id).where(Follow.follower_id == viewer_id, Follow.status == "accepted")
        ).scalars().all()
        engaged = set()
        for model in (Like, Save):
            rows = db.execute(
                select(model.target_type, model.target_id)
                .where(model.user_id == viewer_id)
                .order_by(model.created_at.desc())
                .limit(ACTIVITY_LOOKBACK)
            ).all()
            piece_ids = [tid for kind, tid in rows if kind == "piece"]
            post_ids = [tid for kind, tid in rows if kind == "post"]
            if piece_ids:
                engaged |= set(db.execute(select(Piece.user_id).where(Piece.id.in_(piece_ids))).scalars())
            if post_ids:
                engaged |= set(db.execute(select(Post.user_id).where(Post.id.in_(post_ids))).scalars())
        engaged.discard(viewer_id)
        viewer = db.get(User, viewer_id)
        taste = (viewer.taste_preferences if viewer is not None else None) or {}
        mediums = taste.get("mediums") or []
        tags = [*(taste.get("styles") or []), *(taste.get("themes") or [])]
        return cls(followed=followed, engaged=engaged, mediums=mediums, tags=tags)


def _variants(values: Iterable[str]) -> list:
    """Each tag as given, lower case and capitalised: tags on pieces are free text."""
    out = set()
    for v in values:
        out |= {v, v.lower(), v[:1].upper() + v[1:].lower()}
    return sorted(out)


def piece_rank(signals: Signals):
    """The For You rank of a piece, as SQL."""
    taste = []
    if signals.mediums:
        mediums = sorted(signals.mediums)
        taste.append(func.lower(Piece.medium).in_(mediums))
        taste.append(Piece.mediums.overlap(literal(_variants(mediums), ARRAY(String))))
    if signals.tags:
        taste.append(Piece.style_tags.overlap(literal(_variants(signals.tags), ARRAY(String))))
    if signals.engaged:
        taste.append(Piece.user_id.in_(signals.engaged))
    return case(
        (Piece.user_id.in_(signals.followed) if signals.followed else false(), RANK_FOLLOWED),
        (or_(*taste) if taste else false(), RANK_TASTE),
        else_=RANK_REST,
    )


def post_rank(signals: Signals):
    """The For You rank of a Scene: by its author (Scenes carry no medium or tags)."""
    return case(
        (Post.user_id.in_(signals.followed) if signals.followed else false(), RANK_FOLLOWED),
        (Post.user_id.in_(signals.engaged) if signals.engaged else false(), RANK_TASTE),
        else_=RANK_REST,
    )


def after_cursor(rank_expr, created_col, id_col, cursor):
    """Rows after `cursor` (rank, created_at, id) in For You order: a later rank, or the same
    rank and older."""
    rank, created_at, item_id = cursor
    return or_(
        rank_expr > rank,
        and_(rank_expr == rank, tuple_(created_col, id_col) < tuple_(created_at, item_id)),
    )


def encode_cursor(rank: int, created_at: datetime, item_id) -> str:
    raw = f"r{rank}|{created_at.isoformat()}|{item_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str):
    """`(rank, created_at, id)`, or None for nonsense. A cursor from before For You was ranked
    (`created_at|id`) reads as the first rank, so a page already scrolled to goes on."""
    try:
        parts = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        if len(parts) == 3 and parts[0].startswith("r"):
            return int(parts[0][1:]), datetime.fromisoformat(parts[1]), uuid.UUID(parts[2])
        if len(parts) == 2:
            return RANK_FOLLOWED, datetime.fromisoformat(parts[0]), uuid.UUID(parts[1])
    except Exception:
        return None
    return None


def order_ranked(rows: list) -> list:
    """`[(rank, created_at, id, item)]` in For You order: rank, then newest, then id."""
    by_time = sorted(rows, key=lambda r: (r[1], str(r[2])), reverse=True)
    return sorted(by_time, key=lambda r: r[0])
