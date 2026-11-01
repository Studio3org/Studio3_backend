"""Notifications for people watching a piece or an artist (BACKEND_CHANGES B12).

`NotifSettings` "Saved pieces" (Now listed, price changes, auctions starting) and "Artists you
follow" (New pieces and Scenes). Everyone who saved a piece hears when it goes on sale, its
price changes, or it goes to auction; everyone who follows an artist hears when they post a
piece or a Scene.

Each goes through `create_and_push`, so the person's `push[type]` setting turns it off. Each is
one row a day: a second price change, or a second post from the same artist, the same day
updates that row (and brings it back to the top, unread) without another push, so a batch
upload is one notification.

Call these after the caller's commit, like `rsvp_service.notify_attendees`: a failure is
logged, never raised, so a listing or a post is never undone by a notification.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.modules.notifications import notifications_dao
from src.modules.pieces import piece_state
from src.shared.models.block import Block
from src.shared.models.notification import Notification
from src.shared.models.piece import Piece
from src.shared.models.social import Collection, CollectionItem, Follow, Save
from src.shared.models.user import User
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

SAVED_PIECE_LISTED = "saved_piece_listed"
SAVED_PIECE_PRICE = "saved_piece_price"
SAVED_PIECE_AUCTION = "saved_piece_auction"
FOLLOWED_ARTIST_POST = "followed_artist_post"
SAVED_PIECE_TYPES = (SAVED_PIECE_LISTED, SAVED_PIECE_PRICE, SAVED_PIECE_AUCTION)


def money(cents: Optional[int]) -> str:
    """"$950", or "$950.50" when there are cents."""
    if cents is None:
        return ""
    dollars, rest = divmod(int(cents), 100)
    return f"${dollars:,}" if rest == 0 else f"${dollars:,}.{rest:02d}"


# --- who is watching ----------------------------------------------------------------------

def _not_blocked_with(person_id: uuid.UUID):
    """Users with no block either way with `person_id`."""
    return ~User.id.in_(
        select(Block.blocked_id).where(Block.blocker_id == person_id)
        .union(select(Block.blocker_id).where(Block.blocked_id == person_id))
    )


def savers_of(db: Session, piece: Piece) -> list[uuid.UUID]:
    """Everyone who saved the piece, quickly or into a collection; not its artist, no deleted
    accounts, nobody either side has blocked."""
    quick = select(Save.user_id).where(Save.target_type == "piece", Save.target_id == piece.id)
    in_collection = (
        select(Collection.user_id)
        .join(CollectionItem, CollectionItem.collection_id == Collection.id)
        .where(CollectionItem.target_type == "piece", CollectionItem.target_id == piece.id)
    )
    return list(
        db.execute(
            select(User.id).where(
                or_(User.id.in_(quick), User.id.in_(in_collection)),
                User.id != piece.user_id,
                User.deleted_at.is_(None),
                _not_blocked_with(piece.user_id),
            )
        ).scalars().all()
    )


def followers_of(db: Session, artist_id: uuid.UUID) -> list[uuid.UUID]:
    """Accepted followers (not pending requests), no deleted accounts, no blocks."""
    return list(
        db.execute(
            select(User.id).where(
                User.id.in_(
                    select(Follow.follower_id).where(
                        Follow.following_id == artist_id, Follow.status == "accepted"
                    )
                ),
                User.id != artist_id,
                User.deleted_at.is_(None),
                _not_blocked_with(artist_id),
            )
        ).scalars().all()
    )


# --- one row a day ------------------------------------------------------------------------

def _todays_row(db: Session, user_id, type: str, *, target_id=None, actor_id=None) -> Optional[Notification]:
    query = db.query(Notification).filter(
        Notification.user_id == user_id,
        Notification.type == type,
        Notification.created_at >= notifications_dao._day_start(),
    )
    if target_id is not None:
        query = query.filter(Notification.target_id == target_id)
    if actor_id is not None:
        query = query.filter(Notification.actor_id == actor_id)
    return query.order_by(Notification.created_at.desc()).first()


def _refresh(db: Session, row: Notification, payload: dict, target_id=None) -> None:
    """Today's row takes the news and comes back to the top, unread; no second push."""
    row.payload = {**(row.payload or {}), **payload}
    if target_id is not None:
        row.target_id = target_id
    row.created_at = datetime.now(timezone.utc)
    row.read = False
    db.commit()


# --- saved pieces -------------------------------------------------------------------------

@dataclass(frozen=True)
class ListingState:
    """What a saver cares about, taken before a change so it can be compared after."""

    status: Optional[str]
    is_for_sale: bool
    listing_type: Optional[str]
    price_cents: Optional[int]

    @property
    def buyable(self) -> bool:
        """Live and for sale at a fixed price (auctions are told apart, when they open)."""
        return self.status == piece_state.LIVE and self.is_for_sale and self.listing_type != "auction"


def listing_state(piece: Piece) -> ListingState:
    return ListingState(
        status=piece.status,
        is_for_sale=bool(piece.is_for_sale),
        listing_type=piece.listing_type,
        price_cents=piece.price_cents,
    )


def saved_piece_message(type: str, title: str, payload: dict) -> str:
    """The row's text (`NotifActivity`: "Paper Planes III, which you saved, is now available
    for $950.")."""
    price = money(payload.get("priceCents"))
    if type == SAVED_PIECE_LISTED:
        return f"{title}, which you saved, is now available for {price}."
    if type == SAVED_PIECE_PRICE:
        was = payload.get("wasPriceCents")
        if was is None or was == payload.get("priceCents"):
            return f"{title}, which you saved, is now {price}."
        return f"{title}, which you saved, is now {price} (was {money(was)})."
    if payload.get("biddingOpen"):
        return f"{title}, which you saved, is up for auction. Bidding starts at {price}."
    return f"{title}, which you saved, is going to auction. Bidding starts at {price}."


def _tell_savers(db: Session, piece: Piece, type: str, payload: dict) -> int:
    title = (piece.title or "A piece").strip() or "A piece"
    told = 0
    for user_id in savers_of(db, piece):
        try:
            row = _todays_row(db, user_id, type, target_id=piece.id)
            if row is not None:
                news = dict(payload)
                if type == SAVED_PIECE_PRICE and "wasPriceCents" in (row.payload or {}):
                    # Against the price they last heard about, not the one in between.
                    news["wasPriceCents"] = row.payload["wasPriceCents"]
                news["message"] = saved_piece_message(type, title, news)
                _refresh(db, row, news)
            else:
                message = saved_piece_message(type, title, payload)
                notifications_dao.create_and_push(
                    db,
                    user_id=user_id,
                    type=type,
                    actor_id=piece.user_id,
                    target_type="piece",
                    target_id=piece.id,
                    title="A piece you saved",
                    body=message,
                    payload={**payload, "message": message},
                )
            told += 1
        except Exception:
            db.rollback()
            logger.exception("Saved-piece notification %s failed for %s on %s", type, user_id, piece.id)
    return told


def after_listing_change(db: Session, piece: Piece, before: ListingState) -> int:
    """Tells savers when the piece has just gone on sale, or its price moved while on sale.
    Returns how many were told."""
    now = listing_state(piece)
    if now.buyable and not before.buyable:
        return _tell_savers(db, piece, SAVED_PIECE_LISTED, {"priceCents": now.price_cents})
    if now.buyable and before.buyable and now.price_cents != before.price_cents:
        return _tell_savers(
            db, piece, SAVED_PIECE_PRICE,
            {"priceCents": now.price_cents, "wasPriceCents": before.price_cents},
        )
    return 0


def auction_opened(db: Session, piece: Piece, auction) -> int:
    """Tells savers the piece is going to auction (now, or when its event starts)."""
    opens_at = getattr(auction, "opens_at", None)
    bidding_open = opens_at is None or opens_at <= datetime.now(timezone.utc)
    payload = {"priceCents": auction.starting_bid_cents, "biddingOpen": bidding_open}
    if opens_at is not None:
        payload["opensAt"] = opens_at.isoformat()
    return _tell_savers(db, piece, SAVED_PIECE_AUCTION, payload)


# --- artists you follow -------------------------------------------------------------------

def _count_phrase(count: int, noun: str) -> str:
    return f"a new {noun}" if count == 1 else f"{count} new {noun}s"


def artist_post_message(name: str, pieces: int, scenes: int) -> str:
    """"Amara Osei posted 3 new pieces." / "… a new Scene." / "… 2 new pieces and a new Scene."""
    parts = []
    if pieces:
        parts.append(_count_phrase(pieces, "piece"))
    if scenes:
        parts.append(_count_phrase(scenes, "Scene"))
    return f"{name} posted {' and '.join(parts) or 'something new'}."


def artist_posted(db: Session, artist_id: uuid.UUID, kind: str, target_id: uuid.UUID) -> int:
    """Tells followers the artist posted a piece (`kind="piece"`) or a Scene (`"scene"`). The
    first post of the day pushes; later ones that day update the same row."""
    artist = db.get(User, artist_id)
    if artist is None:
        return 0
    name = artist.name or artist.username or "An artist"
    target_type = "piece" if kind == "piece" else "post"
    told = 0
    for user_id in followers_of(db, artist_id):
        try:
            row = _todays_row(db, user_id, FOLLOWED_ARTIST_POST, actor_id=artist_id)
            if row is not None:
                current = row.payload or {}
                pieces = int(current.get("pieces") or 0) + (kind == "piece")
                scenes = int(current.get("scenes") or 0) + (kind != "piece")
                row.target_type = target_type
                _refresh(
                    db, row,
                    {"pieces": pieces, "scenes": scenes, "message": artist_post_message(name, pieces, scenes)},
                    target_id=target_id,
                )
            else:
                pieces, scenes = int(kind == "piece"), int(kind != "piece")
                message = artist_post_message(name, pieces, scenes)
                notifications_dao.create_and_push(
                    db,
                    user_id=user_id,
                    type=FOLLOWED_ARTIST_POST,
                    actor_id=artist_id,
                    target_type=target_type,
                    target_id=target_id,
                    title=name,
                    body=message,
                    payload={"pieces": pieces, "scenes": scenes, "message": message},
                )
            told += 1
        except Exception:
            db.rollback()
            logger.exception("Followed-artist notification failed for %s on %s", user_id, target_id)
    return told
