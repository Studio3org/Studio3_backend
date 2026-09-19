"""Notifications DAO — create, list (cursor), read state, and the push-dispatch helper."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select, func, update
from sqlalchemy.orm import Session

from src.shared.models.notification import Notification
from src.shared.models.user import User
from src.shared.notification.push_service import send_push

# Activity types that belong in the Notifications tab. Chat DMs (`message`) are
# excluded — they surface as phone push + Chats unread badge only (Instagram-style).
_ACTIVITY_FEED_EXCLUDED_TYPES = ("message",)


def create_notification(
    db: Session,
    user_id: uuid.UUID,
    type: str,
    actor_id: Optional[uuid.UUID] = None,
    target_type: Optional[str] = None,
    target_id: Optional[uuid.UUID] = None,
    payload: Optional[dict] = None,
) -> Notification:
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
    so this never rolls back the notification."""
    notification = create_notification(db, user_id, type, actor_id, target_type, target_id, payload)
    recipient = db.get(User, user_id)
    # Unset notification_preferences (never configured) defaults to all-enabled.
    push_prefs = (recipient.notification_preferences or {}).get("push", {}) if recipient else {}
    if push_prefs.get(type, True):
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
    payload = notification.payload or {}
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
