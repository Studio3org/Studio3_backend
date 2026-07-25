"""Socket.IO event handlers for real-time chat delivery, typing indicators, and presence.

Persistence always goes through chat_controller/chat_dao (the same functions the REST
`POST /api/conversations/<id>/messages` route calls) so there is a single source of truth for
business rules (block checks, pending/accept, read state) regardless of transport.
"""
import uuid

from flask import request
from flask_socketio import join_room, leave_room, emit

from src.shared.config.database import SessionLocal
from src.shared.config.redis_client import get_redis_client
from src.shared.realtime.socketio_instance import socketio
from src.shared.utils.jwt_utils import verify_access_token
from src.modules.chat import chat_dao
from src.modules.user.user_dao import get_user_by_id

PRESENCE_TTL_SECONDS = 45


def _user_room(user_id) -> str:
    return f"user:{user_id}"


def _conversation_room(conversation_id) -> str:
    return f"conversation:{conversation_id}"


def _authenticate() -> str | None:
    token = request.args.get("token")
    if not token:
        return None
    payload = verify_access_token(token)
    if not payload:
        return None
    return payload.get("sub")


def _mark_online(user_id: str) -> None:
    try:
        get_redis_client().setex(f"presence:{user_id}", PRESENCE_TTL_SECONDS, "1")
    except Exception:
        pass  # Presence is best-effort; never block the socket on Redis hiccups.


def _mark_offline(user_id: str) -> None:
    try:
        get_redis_client().delete(f"presence:{user_id}")
    except Exception:
        pass


@socketio.on("connect")
def on_connect():
    user_id = _authenticate()
    if not user_id:
        return False  # reject the handshake
    join_room(_user_room(user_id))
    _mark_online(user_id)
    # Broadcast globally rather than to _user_room(user_id) — no one else is ever in that
    # room, so a room-scoped emit here would never reach anyone.
    emit("presence:update", {"userId": user_id, "online": True}, broadcast=True, include_self=False)


@socketio.on("disconnect")
def on_disconnect():
    user_id = _authenticate()
    if user_id:
        _mark_offline(user_id)
        emit("presence:update", {"userId": user_id, "online": False}, broadcast=True)


@socketio.on("conversation:join")
def on_join(data):
    user_id = _authenticate()
    conversation_id = (data or {}).get("conversationId")
    if not user_id or not conversation_id:
        return
    db = SessionLocal()
    try:
        conversation = chat_dao.get_conversation(db, uuid.UUID(conversation_id))
        if not conversation:
            return
        if str(conversation.participant_one_id) != user_id and str(conversation.participant_two_id) != user_id:
            return
        join_room(_conversation_room(conversation_id))
    finally:
        db.close()


@socketio.on("conversation:leave")
def on_leave(data):
    conversation_id = (data or {}).get("conversationId")
    if conversation_id:
        leave_room(_conversation_room(conversation_id))


@socketio.on("typing:start")
def on_typing_start(data):
    user_id = _authenticate()
    conversation_id = (data or {}).get("conversationId")
    if user_id and conversation_id:
        emit("typing:start", {"conversationId": conversation_id, "userId": user_id}, room=_conversation_room(conversation_id), include_self=False)


@socketio.on("typing:stop")
def on_typing_stop(data):
    user_id = _authenticate()
    conversation_id = (data or {}).get("conversationId")
    if user_id and conversation_id:
        emit("typing:stop", {"conversationId": conversation_id, "userId": user_id}, room=_conversation_room(conversation_id), include_self=False)


@socketio.on("message:send")
def on_message_send(data):
    """Applies the same rules as chat_controller.send_message (the REST handler), but persists
    directly via chat_dao since the HTTP request body/g.user context isn't available here."""
    user_id = _authenticate()
    conversation_id = (data or {}).get("conversationId")
    body = (data or {}).get("body")
    image_url = (data or {}).get("imageUrl")
    if not user_id or not conversation_id:
        emit("message:error", {"error": "Not authenticated or missing conversationId."})
        return
    if not body and not image_url:
        emit("message:error", {"error": "body or imageUrl is required."})
        return

    db = SessionLocal()
    try:
        conversation = chat_dao.get_conversation(db, uuid.UUID(conversation_id))
        if not conversation or (
            str(conversation.participant_one_id) != user_id and str(conversation.participant_two_id) != user_id
        ):
            emit("message:error", {"error": "Conversation not found."})
            return
        if conversation.status == "closed":
            emit("message:error", {"error": "This conversation is closed."})
            return
        if conversation.status == "pending":
            chat_dao.accept_conversation(db, conversation)
        message = chat_dao.create_message(db, conversation, uuid.UUID(user_id), body, image_url)
        sender = get_user_by_id(db, uuid.UUID(user_id))
        payload = chat_dao.message_to_dict(message, sender)
        payload["conversationId"] = str(conversation.id)
        other_id = chat_dao.other_participant_id(conversation, uuid.UUID(user_id))

        from src.modules.notifications import notifications_dao
        notifications_dao.push_only(
            db,
            user_id=other_id,
            type="message",
            title="New message",
            body=f"{sender.name}: {(body or 'Sent a photo')[:100]}",
            push_data={
                "type": "message",
                "conversationId": str(conversation.id),
            },
        )
    finally:
        db.close()

    emit("message:new", payload, room=_conversation_room(conversation_id))
    emit("message:new", payload, room=_user_room(str(other_id)))


def _target_room(target_type: str, target_id: str) -> str:
    return f"target:{target_type}:{target_id}"


@socketio.on("target:join")
def on_target_join(data):
    """Join a post/piece room to receive live `comment:new` events."""
    user_id = _authenticate()
    target_type = (data or {}).get("targetType")
    target_id = (data or {}).get("targetId")
    if not user_id or not target_type or not target_id:
        return
    if target_type not in ("post", "piece"):
        return
    join_room(_target_room(target_type, str(target_id)))


@socketio.on("target:leave")
def on_target_leave(data):
    target_type = (data or {}).get("targetType")
    target_id = (data or {}).get("targetId")
    if target_type and target_id:
        leave_room(_target_room(target_type, str(target_id)))
