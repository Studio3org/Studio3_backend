"""Chat controller — general-purpose 1:1 direct messages (Instagram-style requests)."""
import base64
import uuid
from datetime import datetime

from flask import request, g
from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.realtime.socketio_instance import socketio
from src.modules.user.user_dao import get_user_by_id
from src.modules.auth.auth_dao import find_user_by_username
from src.modules.chat import chat_dao
from src.modules.notifications import notifications_dao
from src.modules.social import social_dao
from src.modules.social import block_dao


def _broadcast_new_message(conversation_id, recipient_id, message_dict):
    """Also emit over Socket.IO so a message sent via this REST endpoint reaches an already
    -open thread / inbox in real time, same as one sent via the `message:send` socket event."""
    payload = dict(message_dict)
    payload["conversationId"] = str(conversation_id)
    socketio.emit("message:new", payload, room=f"conversation:{conversation_id}")
    socketio.emit("message:new", payload, room=f"user:{recipient_id}")


def _broadcast_conversation_read(conversation_id, reader_id, other_id, read_at):
    payload = {
        "conversationId": str(conversation_id),
        "readerId": str(reader_id),
        "readAt": read_at.isoformat(),
    }
    socketio.emit("conversation:read", payload, room=f"conversation:{conversation_id}")
    socketio.emit("conversation:read", payload, room=f"user:{other_id}")


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


def _require_participant(conversation, user_id):
    if not conversation or (conversation.participant_one_id != user_id and conversation.participant_two_id != user_id):
        raise AppError("Conversation not found.", 404)


def search_users():
    query = (request.args.get("q") or "").strip()
    if not query:
        return {"items": []}, 200
    limit = min(int(request.args.get("limit", 20)), 50)
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        users = chat_dao.search_users(db, me_id, query, limit=limit)
        items = [
            {"username": u.username, "name": u.name, "profilePhotoUrl": u.image}
            for u in users
        ]
        return {"items": items}, 200
    finally:
        db.close()


def find_with_user(username: str):
    """Look up an existing open/pending conversation with `username` (compose reuse)."""
    other_username = (username or "").strip()
    if not other_username:
        raise AppError("username is required.", 400)
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        other = find_user_by_username(db, other_username)
        if not other or other.id == me_id:
            return {"conversation": None}, 200
        if block_dao.is_blocked_either_way(db, me_id, other.id):
            return {"conversation": None}, 200
        existing = chat_dao.get_conversation_between(db, me_id, other.id)
        if not existing or existing.status == "closed":
            return {"conversation": None}, 200
        last_msg = chat_dao.list_messages(db, existing.id, limit=1)
        preview = (
            last_msg[0].body[:140] if last_msg and last_msg[0].body
            else ("Photo" if last_msg and last_msg[0].image_url else None)
        )
        last_sender = last_msg[0].sender_id if last_msg else None
        return {
            "conversation": chat_dao.conversation_to_inbox_dict(
                existing, me_id, other, preview, last_sender_id=last_sender
            )
        }, 200
    finally:
        db.close()


def list_inbox():
    cursor = request.args.get("cursor")
    limit = min(int(request.args.get("limit", 20)), 50)
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        before = _decode_cursor(cursor) if cursor else None
        conversations = chat_dao.list_inbox(db, me_id, limit=limit + 1, before=before)
        has_more = len(conversations) > limit
        conversations = conversations[:limit]
        items = []
        for conversation in conversations:
            other_id = chat_dao.other_participant_id(conversation, me_id)
            other_user = get_user_by_id(db, other_id)
            last_msg = chat_dao.list_messages(db, conversation.id, limit=1)
            preview = (
                last_msg[0].body[:140] if last_msg and last_msg[0].body
                else ("Photo" if last_msg and last_msg[0].image_url else None)
            )
            last_sender = last_msg[0].sender_id if last_msg else None
            items.append(
                chat_dao.conversation_to_inbox_dict(
                    conversation, me_id, other_user, preview, last_sender_id=last_sender
                )
            )
        next_cursor = (
            _encode_cursor(conversations[-1].last_message_at, conversations[-1].id)
            if has_more and conversations
            else None
        )
        return {"items": items, "nextCursor": next_cursor}, 200
    finally:
        db.close()


def list_requests():
    cursor = request.args.get("cursor")
    limit = min(int(request.args.get("limit", 20)), 50)
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        before = _decode_cursor(cursor) if cursor else None
        conversations = chat_dao.list_requests(db, me_id, limit=limit + 1, before=before)
        has_more = len(conversations) > limit
        conversations = conversations[:limit]
        items = []
        for conversation in conversations:
            other_id = chat_dao.other_participant_id(conversation, me_id)
            other_user = get_user_by_id(db, other_id)
            last_msg = chat_dao.list_messages(db, conversation.id, limit=1)
            preview = (
                last_msg[0].body[:140] if last_msg and last_msg[0].body
                else ("Photo" if last_msg and last_msg[0].image_url else None)
            )
            last_sender = last_msg[0].sender_id if last_msg else None
            items.append(
                chat_dao.conversation_to_inbox_dict(
                    conversation, me_id, other_user, preview, last_sender_id=last_sender
                )
            )
        next_cursor = (
            _encode_cursor(conversations[-1].last_message_at, conversations[-1].id)
            if has_more and conversations
            else None
        )
        return {"items": items, "nextCursor": next_cursor}, 200
    finally:
        db.close()


def get_thread(conversation_id: str):
    cursor = request.args.get("cursor")
    limit = min(int(request.args.get("limit", 50)), 100)
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        conversation = chat_dao.get_conversation(db, uuid.UUID(conversation_id))
        _require_participant(conversation, me_id)
        before = datetime.fromisoformat(cursor) if cursor else None
        messages = chat_dao.list_messages(db, conversation.id, limit=limit + 1, before=before)
        has_more = len(messages) > limit
        messages = messages[:limit]
        sender_ids = {m.sender_id for m in messages}
        senders = {}
        if sender_ids:
            senders = {u.id: u for u in db.execute(select(User).where(User.id.in_(sender_ids))).scalars()}
        items = [chat_dao.message_to_dict(m, senders.get(m.sender_id)) for m in messages]
        next_cursor = messages[-1].created_at.isoformat() if has_more and messages else None
        read_at = chat_dao.mark_read(db, conversation, me_id)
        other_id = chat_dao.other_participant_id(conversation, me_id)
        _broadcast_conversation_read(conversation.id, me_id, other_id, read_at)
        other_user = get_user_by_id(db, other_id)
        other_party = None
        other_read = chat_dao.other_party_read_at(conversation, me_id)
        if other_user:
            from src.modules.pieces.pieces_dao import count_user_pieces

            other_party = {
                "id": str(other_user.id),
                "username": other_user.username,
                "name": other_user.name,
                "profilePhotoUrl": other_user.image,
                "followersCount": social_dao.count_followers(db, other_user.id),
                "piecesCount": count_user_pieces(db, other_user.id),
                "isFollowing": social_dao.user_follows(db, me_id, other_user.id),
            }
        return {
            "id": str(conversation.id),
            "otherParty": other_party,
            "otherPartyReadAt": other_read.isoformat() if other_read else None,
            "status": conversation.status,
            "messages": {"items": items, "nextCursor": next_cursor},
        }, 200
    finally:
        db.close()


def unread_count():
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        return {"count": chat_dao.count_unread_inbox(db, me_id)}, 200
    finally:
        db.close()


def _notify_new_message(db, conversation_id, recipient_id, sender, text, is_new_thread):
    # Instagram-style: phone push + Chats unread badge only — no in-app Notifications row.
    notifications_dao.push_only(
        db,
        user_id=recipient_id,
        type="message",
        title="New message request" if is_new_thread else "New message",
        body=f"{sender.name}: {(text or 'Sent a photo')[:100]}",
        push_data={
            "type": "message",
            "conversationId": str(conversation_id),
        },
    )


def start_conversation():
    body = request.get_json() or {}
    other_username = (body.get("username") or "").strip()
    message_text = (body.get("message") or "").strip()
    image_url = (body.get("imageUrl") or "").strip() or None
    if not other_username or (not message_text and not image_url):
        raise AppError("username and message (or imageUrl) are required.", 400)
    db = SessionLocal()
    try:
        me = get_user_by_id(db, uuid.UUID(g.user["id"]))
        other = find_user_by_username(db, other_username)
        if not other:
            raise AppError("User not found.", 404)
        if other.id == me.id:
            raise AppError("Cannot message yourself.", 400)
        if block_dao.is_blocked_either_way(db, me.id, other.id):
            raise AppError("User not found.", 404)

        other_follows_me = social_dao.user_follows(db, other.id, me.id)

        if other.message_permission == "no_one":
            raise AppError("This user isn't accepting messages right now.", 403)
        if other.message_permission == "following" and not other_follows_me:
            raise AppError("This user only accepts messages from people they follow.", 403)

        existing = chat_dao.get_conversation_between(db, me.id, other.id)
        if existing and existing.status != "closed":
            message = chat_dao.create_message(db, existing, me.id, message_text or None, image_url)
            _notify_new_message(db, existing.id, other.id, me, message_text, is_new_thread=False)
            _broadcast_new_message(existing.id, other.id, chat_dao.message_to_dict(message, me))
            return {"id": str(existing.id), "reused": True}, 200

        # Instagram-style requests: if the recipient doesn't already follow the sender, the
        # thread starts "pending" (recipient's Requests folder) until they accept or reply.
        # A previously-closed thread between the same pair is reopened (a unique constraint on
        # the participant pair means we can never create a second row for the same two people).
        initial_status = "open" if other_follows_me else "pending"
        if existing:
            existing.status = initial_status
            conversation = existing
        else:
            conversation = chat_dao.create_conversation(db, me.id, other.id, status=initial_status)
        message = chat_dao.create_message(db, conversation, me.id, message_text[:2000] if message_text else None, image_url)
        _notify_new_message(db, conversation.id, other.id, me, message_text, is_new_thread=(initial_status == "pending"))
        _broadcast_new_message(conversation.id, other.id, chat_dao.message_to_dict(message, me))
        return {"id": str(conversation.id), "reused": False, "status": conversation.status}, 201
    finally:
        db.close()


def send_message(conversation_id: str):
    body = request.get_json() or {}
    text = (body.get("body") or "").strip() or None
    image_url = (body.get("imageUrl") or "").strip() or None
    if not text and not image_url:
        raise AppError("body or imageUrl is required.", 400)
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        conversation = chat_dao.get_conversation(db, uuid.UUID(conversation_id))
        _require_participant(conversation, me_id)
        if conversation.status == "closed":
            raise AppError("This conversation is closed.", 400)
        if conversation.status == "pending":
            # Replying to a pending request implicitly accepts it (matches Instagram).
            chat_dao.accept_conversation(db, conversation)
        message = chat_dao.create_message(db, conversation, me_id, text[:2000] if text else None, image_url)
        other_id = chat_dao.other_participant_id(conversation, me_id)
        sender = get_user_by_id(db, me_id)
        _notify_new_message(db, conversation.id, other_id, sender, text, is_new_thread=False)
        message_dict = chat_dao.message_to_dict(message, sender)
        _broadcast_new_message(conversation.id, other_id, message_dict)
        return message_dict, 201
    finally:
        db.close()


def accept(conversation_id: str):
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        conversation = chat_dao.get_conversation(db, uuid.UUID(conversation_id))
        _require_participant(conversation, me_id)
        if conversation.status != "pending":
            raise AppError(f"Cannot accept a conversation in status '{conversation.status}'.", 400)
        chat_dao.accept_conversation(db, conversation)
        return {"status": "open"}, 200
    finally:
        db.close()


def decline(conversation_id: str):
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        conversation = chat_dao.get_conversation(db, uuid.UUID(conversation_id))
        _require_participant(conversation, me_id)
        if conversation.status != "pending":
            raise AppError(f"Cannot decline a conversation in status '{conversation.status}'.", 400)
        chat_dao.decline_conversation(db, conversation)
        return {"status": "closed"}, 200
    finally:
        db.close()


def mark_read(conversation_id: str):
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        conversation = chat_dao.get_conversation(db, uuid.UUID(conversation_id))
        _require_participant(conversation, me_id)
        read_at = chat_dao.mark_read(db, conversation, me_id)
        other_id = chat_dao.other_participant_id(conversation, me_id)
        _broadcast_conversation_read(conversation.id, me_id, other_id, read_at)
        return {"read": True, "readAt": read_at.isoformat()}, 200
    finally:
        db.close()
