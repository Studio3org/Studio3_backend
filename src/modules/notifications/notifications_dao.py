"""Notifications DAO — create, list (cursor), read state, and the push-dispatch helper."""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func, update
from sqlalchemy.orm import Session

from src.shared.models.inquiry import Inquiry
from src.shared.models.notification import Notification
from src.shared.models.order import OrderItem
from src.shared.models.piece import Piece
from src.shared.models.piece_media import PieceMedia
from src.shared.models.post import Post
from src.shared.models.user import User
from src.shared.notification.push_service import send_push

# Activity types that belong in the Notifications tab. Chat DMs (`message`) are
# excluded — they surface as phone push + Chats unread badge only (Instagram-style).
_ACTIVITY_FEED_EXCLUDED_TYPES = ("message",)


def piece_cover_url(db: Session, piece: Piece) -> Optional[str]:
    """A picture of the piece: its cover, or for a video cover its first photo (none if it
    has no photo — a video URL would not draw as a picture). Also used by chat (B9)."""
    if (piece.media_type or "image") != "video":
        return piece.media_url
    photo = (
        db.query(PieceMedia)
        .filter(PieceMedia.piece_id == piece.id, PieceMedia.media_type == "image")
        .order_by(PieceMedia.sort_order)
        .first()
    )
    return photo.media_url if photo else None


def _piece_fields(db: Session, piece_id: Optional[uuid.UUID]) -> dict:
    piece = db.get(Piece, piece_id) if piece_id else None
    if piece is None:
        return {}
    fields = {"pieceId": str(piece.id), "pieceTitle": piece.title}
    cover = piece_cover_url(db, piece)
    if cover:
        fields["pieceImageUrl"] = cover
    return fields


def _subject_fields(db: Session, target_type: Optional[str], target_id: Optional[uuid.UUID]) -> dict:
    """The piece a notification is about, so the Activity row can name and show it without
    fetching it (BACKEND_CHANGES B6): `pieceId`, `pieceTitle`, `pieceImageUrl`, and for a Scene
    `postImageUrl`. A piece target is the piece; a Scene adds its own picture and its linked
    piece; an order or an inquiry gives its piece. Anything else (users, events) adds nothing.

    Read here, at send time, rather than at each call site: every sender goes through
    `create_notification`, so one place covers likes, saves, comments, purchases, order
    updates and auction notices alike.
    """
    if not target_id:
        return {}
    if target_type == "piece":
        return _piece_fields(db, target_id)
    if target_type == "post":
        post = db.get(Post, target_id)
        if post is None:
            return {}
        fields = _piece_fields(db, post.linked_piece_id)
        image = post.thumbnail_url or (post.media_url if post.media_type == "image" else None)
        if image:
            fields["postImageUrl"] = image
        return fields
    if target_type == "order":
        item = (
            db.query(OrderItem)
            .filter(OrderItem.order_id == target_id)
            .order_by(OrderItem.created_at)
            .first()
        )
        return _piece_fields(db, item.piece_id) if item else {}
    if target_type == "inquiry":
        inquiry = db.get(Inquiry, target_id)
        return _piece_fields(db, inquiry.piece_id) if inquiry else {}
    return {}


# Saves and follows bundle (BACKEND_CHANGES B7): one row per piece per day for saves, one row
# per day for follows, with `payload.count` people and the latest 3 in `payload.actors`.
BUNDLED_TYPES = ("save", "follow")
SUMMARY_DAILY = "daily"
SUMMARY_INSTANT = "instant"
SUMMARY_MODES = (SUMMARY_DAILY, SUMMARY_INSTANT)
# People remembered per bundle so the same person isn't counted twice; past this the count
# still grows, it just can't tell a repeat.
_BUNDLE_ACTOR_IDS_CAP = 200
# Bookkeeping kept in the stored payload that the app never needs.
_INTERNAL_PAYLOAD_KEYS = ("actorIds", "summaryPending")


def is_bundled(type: str, payload: Optional[dict]) -> bool:
    """Saves, and follows except "accepted your follow request" (that one is about you)."""
    if type not in BUNDLED_TYPES:
        return False
    return not (type == "follow" and (payload or {}).get("accepted"))


def saves_and_follows_mode(prefs: Optional[dict]) -> str:
    """`daily` (the default, per `NoteNotifSettings`) or `instant`."""
    mode = (prefs or {}).get("savesAndFollowsMode")
    return mode if mode in SUMMARY_MODES else SUMMARY_DAILY


def _day_start(now: Optional[datetime] = None) -> datetime:
    """Bundles are per UTC day: accounts have no time zone of their own yet."""
    now = now or datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _person(user: Optional[User]) -> Optional[dict]:
    if user is None:
        return None
    return {"username": user.username, "name": user.name, "profilePhotoUrl": user.image}


def bundle_payload(payload: dict, actor_key: Optional[str], person: Optional[dict]) -> tuple[dict, bool]:
    """Adds one person to a bundle's payload. Returns the new payload and whether they were
    new to it (someone already counted today changes nothing)."""
    ids = list(payload.get("actorIds") or [])
    if actor_key and actor_key in ids:
        return payload, False
    count = int(payload.get("count") or max(len(ids), 1)) + 1
    actors = [
        a for a in (payload.get("actors") or [])
        if not person or a.get("username") != person.get("username")
    ]
    if person:
        actors = [person, *actors]
    if actor_key:
        ids = [actor_key, *ids][:_BUNDLE_ACTOR_IDS_CAP]
    return {**payload, "count": count, "actors": actors[:3], "actorIds": ids}, True


def bundle_message(type: str, count: int, first_name: str, target_type: Optional[str]) -> str:
    """The row's text once more than one person is in it (`NotifActivity`: "12 people saved
    your piece …", "Cedric Okafor and 4 others followed you.")."""
    if type == "save":
        what = "Scene" if target_type == "post" else "piece"
        return f"{count} people saved your {what}."
    others = count - 1
    return f"{first_name} and {others} {'other' if others == 1 else 'others'} followed you."


def _add_to_bundle(
    db: Session,
    user_id: uuid.UUID,
    type: str,
    actor_id: Optional[uuid.UUID],
    target_type: Optional[str],
    target_id: Optional[uuid.UUID],
    body: str,
    payload: Optional[dict],
    pending: bool,
) -> tuple[Notification, bool]:
    """Adds this save or follow to today's bundle, or starts one. Returns the row and whether
    the person was new to it."""
    actor = db.get(User, actor_id) if actor_id else None
    person = _person(actor)
    actor_key = str(actor_id) if actor_id else None
    query = db.query(Notification).filter(
        Notification.user_id == user_id,
        Notification.type == type,
        Notification.created_at >= _day_start(),
    )
    if type == "save":
        query = query.filter(Notification.target_type == target_type, Notification.target_id == target_id)
    existing = next(
        (n for n in query.order_by(Notification.created_at.desc()).all() if is_bundled(type, n.payload)),
        None,
    )
    if existing is None:
        stored = {
            "message": body,
            **(payload or {}),
            "count": 1,
            "actors": [person] if person else [],
            "actorIds": [actor_key] if actor_key else [],
        }
        if pending:
            stored["summaryPending"] = True
        return create_notification(db, user_id, type, actor_id, target_type, target_id, stored), True

    current = dict(existing.payload or {})
    if "actorIds" not in current:
        # A row from before bundling: it already holds one person.
        first = db.get(User, existing.actor_id) if existing.actor_id else None
        current["actorIds"] = [str(existing.actor_id)] if existing.actor_id else []
        current["actors"] = [p for p in [_person(first)] if p]
        current["count"] = 1
    updated, someone_new = bundle_payload(current, actor_key, person)
    if not someone_new:
        return existing, False
    first_name = (person or {}).get("name") or "Someone"
    updated["message"] = bundle_message(type, updated["count"], first_name, target_type)
    if pending:
        updated["summaryPending"] = True
    existing.payload = updated
    # The newest person leads, and the row comes back to the top, unread.
    existing.actor_id = actor_id
    if type == "follow":
        existing.target_id = target_id
    existing.created_at = datetime.now(timezone.utc)
    existing.read = False
    db.commit()
    db.refresh(existing)
    return existing, True


def create_notification(
    db: Session,
    user_id: uuid.UUID,
    type: str,
    actor_id: Optional[uuid.UUID] = None,
    target_type: Optional[str] = None,
    target_id: Optional[uuid.UUID] = None,
    payload: Optional[dict] = None,
) -> Notification:
    # What the notification is about comes first, so a caller's own value for a key wins.
    subject = _subject_fields(db, target_type, target_id)
    if subject:
        payload = {**subject, **(payload or {})}
    notification = Notification(
        id=uuid.uuid4(),
        user_id=user_id,
        type=type,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        payload=payload,
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    return notification


def create_and_push(
    db: Session,
    user_id: uuid.UUID,
    type: str,
    title: str,
    body: str,
    actor_id: Optional[uuid.UUID] = None,
    target_type: Optional[str] = None,
    target_id: Optional[uuid.UUID] = None,
    payload: Optional[dict] = None,
    push_data: Optional[dict] = None,
) -> Notification:
    """Writes the Notification row, then attempts a push (unless the recipient has muted this
    type). Push failure never affects the row — send_push always fails open (never raises),
    so this never rolls back the notification.

    `body` is folded into the stored payload as `message` (a caller-supplied
    `payload['message']` wins if one was already given — inquiries set their own, the buyer's
    actual text). Without this, `body` only ever reached the phone push: the in-app
    Notifications list has nothing but `type` and `actor` to go on, and for every type the
    client doesn't special-case — a bid result, an auction closing, an event being cancelled,
    anything with no real actor at all, since these are the app telling *you* something
    happened rather than "someone did X to you" — that rendered as a bare "Someone sent you a
    notification".
    """
    recipient = db.get(User, user_id)
    prefs = (recipient.notification_preferences or {}) if recipient else {}
    # Unset notification_preferences (never configured) defaults to all-enabled.
    push_prefs = prefs.get("push", {})
    bundled = is_bundled(type, payload)
    # Saves and follows go out as one push a day unless the person chose "As they happen".
    daily = bundled and saves_and_follows_mode(prefs) == SUMMARY_DAILY
    if bundled:
        notification, someone_new = _add_to_bundle(
            db, user_id, type, actor_id, target_type, target_id, body, payload, pending=daily
        )
        if not someone_new:
            # The same person again today (unsaved and saved again): nothing new to tell.
            return notification
    else:
        stored_payload = {"message": body, **(payload or {})}
        notification = create_notification(
            db, user_id, type, actor_id, target_type, target_id, stored_payload
        )
    if push_prefs.get(type, True) and not daily:
        send_push(
            user_id, title, body,
            data=push_data or _push_target(db, type, target_type, target_id, actor_id),
        )
    # Live in-app activity (not for DMs — those use push_only).
    if type not in _ACTIVITY_FEED_EXCLUDED_TYPES:
        try:
            from src.shared.realtime.socketio_instance import socketio

            actor = db.get(User, actor_id) if actor_id else None
            socketio.emit(
                "notification:new",
                notification_to_dict(notification, actor),
                room=f"user:{user_id}",
            )
        except Exception:
            pass
    return notification


def _push_target(
    db: Session,
    type: str,
    target_type: Optional[str],
    target_id: Optional[uuid.UUID],
    actor_id: Optional[uuid.UUID],
) -> dict:
    """What the app needs to open the right screen when someone taps the notification.

    Derived here rather than at each call site because it is the same three fields every
    time, and because the alternative was what actually shipped: `push_data` was an optional
    argument that only the two chat paths ever passed, so every other push — a sale, an
    outbid, a declined auction payment with a deadline attached — arrived with an empty data
    payload and nothing for the client to route on.

    `actorUsername` is included because a `user` target is the one case where `targetId` is
    not routable: it is a UUID, while the profile screen is addressed by username.

    Values are strings because FCM stringifies the whole data map anyway; sending them
    already-stringified keeps what the client parses identical to what is written here.
    """
    data = {"type": type}
    if target_type and target_id:
        data["targetType"] = target_type
        data["targetId"] = str(target_id)
    if actor_id:
        actor = db.get(User, actor_id)
        if actor and actor.username:
            data["actorUsername"] = actor.username
    return data


def push_only(
    db: Session,
    user_id: uuid.UUID,
    type: str,
    title: str,
    body: str,
    push_data: Optional[dict] = None,
) -> None:
    """Send a phone push without writing an in-app Notification row.

    Used for DMs (Instagram-style): system push + Chats tab badge only — not the
    Notifications activity feed.
    """
    recipient = db.get(User, user_id)
    push_prefs = (recipient.notification_preferences or {}).get("push", {}) if recipient else {}
    if push_prefs.get(type, True):
        send_push(user_id, title, body, data=push_data)


def list_notifications(
    db: Session, user_id: uuid.UUID, limit: int = 20, before: Optional[datetime] = None
) -> list[Notification]:
    q = select(Notification).where(
        Notification.user_id == user_id,
        Notification.type.notin_(_ACTIVITY_FEED_EXCLUDED_TYPES),
    )
    if before:
        q = q.where(Notification.created_at < before)
    return list(db.execute(q.order_by(Notification.created_at.desc()).limit(limit)).scalars().all())


def mark_read(db: Session, user_id: uuid.UUID, notification_id: uuid.UUID) -> bool:
    notification = db.execute(
        select(Notification).where(Notification.id == notification_id, Notification.user_id == user_id)
    ).scalar_one_or_none()
    if not notification:
        return False
    notification.read = True
    db.commit()
    return True


def mark_all_read(db: Session, user_id: uuid.UUID) -> int:
    result = db.execute(
        update(Notification)
        .where(
            Notification.user_id == user_id,
            Notification.read.is_(False),
            Notification.type.notin_(_ACTIVITY_FEED_EXCLUDED_TYPES),
        )
        .values(read=True)
    )
    db.commit()
    return result.rowcount


def count_unread(db: Session, user_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(Notification.id)).where(
            Notification.user_id == user_id,
            Notification.read.is_(False),
            Notification.type.notin_(_ACTIVITY_FEED_EXCLUDED_TYPES),
        )
    ).scalar_one()


def notification_to_dict(notification: Notification, actor: Optional[User]) -> dict:
    payload = {
        k: v for k, v in (notification.payload or {}).items() if k not in _INTERNAL_PAYLOAD_KEYS
    }
    return {
        "id": str(notification.id),
        "type": notification.type,
        "actor": (
            {"username": actor.username, "name": actor.name, "profilePhotoUrl": actor.image}
            if actor
            else None
        ),
        "target": (
            {"type": notification.target_type, "id": str(notification.target_id)}
            if notification.target_type and notification.target_id
            else None
        ),
        "payload": payload,
        "message": payload.get("message"),
        "read": notification.read,
        "createdAt": notification.created_at.isoformat(),
    }
