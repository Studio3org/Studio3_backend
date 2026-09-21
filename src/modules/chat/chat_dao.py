"""Chat DAO — general-purpose 1:1 conversations."""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, or_, tuple_
from sqlalchemy.orm import Session

from src.shared.models.block import Block
from src.shared.models.chat import Conversation, ChatMessage
from src.shared.models.user import User


def utc_now():
    return datetime.now(timezone.utc)


def _ordered_pair(user_a: uuid.UUID, user_b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (user_a, user_b) if str(user_a) < str(user_b) else (user_b, user_a)


def search_users(db: Session, viewer_id: uuid.UUID, query: str, limit: int = 20) -> list[User]:
    """Users matching `query` (username or name, case-insensitive substring) for the "new
    message" composer — excludes the viewer themself and anyone blocked in either direction.
    Doesn't filter on `message_permission`: that's enforced at send time (start_conversation),
    same as the rest of the app lets you find/view a private account before following it."""
    like = f"%{query.strip()}%"
    blocked_subquery = select(Block.blocked_id).where(Block.blocker_id == viewer_id)
    blocked_by_subquery = select(Block.blocker_id).where(Block.blocked_id == viewer_id)
    q = (
        select(User)
        .where(
            or_(User.username.ilike(like), User.name.ilike(like)),
            User.id != viewer_id,
            User.id.not_in(blocked_subquery),
            User.id.not_in(blocked_by_subquery),
        )
        .order_by(User.username.asc())
        .limit(limit)
    )
    return list(db.execute(q).scalars().all())


def get_conversation_between(db: Session, user_a: uuid.UUID, user_b: uuid.UUID) -> Optional[Conversation]:
    one, two = _ordered_pair(user_a, user_b)
    return db.execute(
        select(Conversation).where(
            Conversation.participant_one_id == one, Conversation.participant_two_id == two
        )
    ).scalar_one_or_none()


def create_conversation(db: Session, user_a: uuid.UUID, user_b: uuid.UUID, status: str = "open") -> Conversation:
    one, two = _ordered_pair(user_a, user_b)
    conversation = Conversation(id=uuid.uuid4(), participant_one_id=one, participant_two_id=two, status=status)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def get_conversation(db: Session, conversation_id: uuid.UUID) -> Optional[Conversation]:
    return db.get(Conversation, conversation_id)


def other_participant_id(conversation: Conversation, viewer_id: uuid.UUID) -> uuid.UUID:
    return (
        conversation.participant_two_id
        if conversation.participant_one_id == viewer_id
        else conversation.participant_one_id
    )


def list_inbox(
    db: Session, user_id: uuid.UUID, limit: int = 20, before: Optional[tuple] = None
) -> list[Conversation]:
    """Open threads for this user (the recipient side of a pending thread only sees it in
    list_requests() instead — matches the Instagram-style "message requests" split)."""
    q = select(Conversation).where(
        or_(Conversation.participant_one_id == user_id, Conversation.participant_two_id == user_id),
        Conversation.status == "open",
    )
    if before:
        before_ts, before_id = before
        q = q.where(tuple_(Conversation.last_message_at, Conversation.id) < tuple_(before_ts, before_id))
    return list(
        db.execute(
            q.order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(limit)
        ).scalars().all()
    )


def list_requests(
    db: Session, user_id: uuid.UUID, limit: int = 20, before: Optional[tuple] = None
) -> list[Conversation]:
    """Pending threads awaiting this user's accept/decline (they're the non-initiating side)."""
    q = select(Conversation).where(
        or_(Conversation.participant_one_id == user_id, Conversation.participant_two_id == user_id),
        Conversation.status == "pending",
    )
    if before:
        before_ts, before_id = before
        q = q.where(tuple_(Conversation.last_message_at, Conversation.id) < tuple_(before_ts, before_id))
    return list(
        db.execute(
            q.order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(limit)
        ).scalars().all()
    )


def accept_conversation(db: Session, conversation: Conversation) -> None:
    conversation.status = "open"
    db.commit()


def decline_conversation(db: Session, conversation: Conversation) -> None:
    conversation.status = "closed"
    db.commit()


def create_message(
    db: Session,
    conversation: Conversation,
    sender_id: uuid.UUID,
    body: Optional[str] = None,
    image_url: Optional[str] = None,
) -> ChatMessage:
    message = ChatMessage(
        id=uuid.uuid4(), conversation_id=conversation.id, sender_id=sender_id, body=body, image_url=image_url
    )
    db.add(message)
    db.flush()  # assign created_at before aligning last_message_at
    conversation.last_message_at = message.created_at
    db.commit()
    db.refresh(message)
    return message


def list_messages(
    db: Session, conversation_id: uuid.UUID, limit: int = 50, before: Optional[datetime] = None
) -> list[ChatMessage]:
    q = select(ChatMessage).where(ChatMessage.conversation_id == conversation_id)
    if before:
        q = q.where(ChatMessage.created_at < before)
    return list(
        db.execute(q.order_by(ChatMessage.created_at.desc()).limit(limit)).scalars().all()
    )


def mark_read(db: Session, conversation: Conversation, reader_id: uuid.UUID) -> datetime:
    """Mark conversation read for reader; returns the new read_at timestamp."""
    now = utc_now()
    if conversation.participant_one_id == reader_id:
        conversation.participant_one_read_at = now
    elif conversation.participant_two_id == reader_id:
        conversation.participant_two_read_at = now
    db.commit()
    return now


def other_party_read_at(conversation: Conversation, viewer_id: uuid.UUID) -> Optional[datetime]:
    if conversation.participant_one_id == viewer_id:
        return conversation.participant_two_read_at
    return conversation.participant_one_read_at


def is_unread_for(
    conversation: Conversation,
    viewer_id: uuid.UUID,
    last_sender_id: Optional[uuid.UUID] = None,
) -> bool:
    """Unread only when the latest message is from the other party and newer than my read_at."""
    if last_sender_id is not None and last_sender_id == viewer_id:
        return False
    my_read_at = (
        conversation.participant_one_read_at
        if conversation.participant_one_id == viewer_id
        else conversation.participant_two_read_at
    )
    if my_read_at is None:
        return True
    return conversation.last_message_at > my_read_at


def count_unread_inbox(db: Session, user_id: uuid.UUID) -> int:
    """Count open conversations with unread messages from the other party."""
    conversations = list(
        db.execute(
            select(Conversation).where(
                or_(Conversation.participant_one_id == user_id, Conversation.participant_two_id == user_id),
                Conversation.status == "open",
            )
        ).scalars().all()
    )
    total = 0
    for conversation in conversations:
        last = list_messages(db, conversation.id, limit=1)
        last_sender = last[0].sender_id if last else None
        if is_unread_for(conversation, user_id, last_sender):
            total += 1
    return total


def conversation_to_inbox_dict(
    conversation: Conversation,
    viewer_id: uuid.UUID,
    other_user: Optional[User],
    preview: Optional[str],
    last_sender_id: Optional[uuid.UUID] = None,
) -> dict:
    other_read = other_party_read_at(conversation, viewer_id)
    return {
        "id": str(conversation.id),
        "otherParty": (
            {
                "id": str(other_user.id),
                "username": other_user.username,
                "name": other_user.name,
                "profilePhotoUrl": other_user.image,
            }
            if other_user
            else None
        ),
        "preview": preview,
        "updatedAt": conversation.last_message_at.isoformat(),
        "unread": is_unread_for(conversation, viewer_id, last_sender_id),
        "status": conversation.status,
        "otherPartyReadAt": other_read.isoformat() if other_read else None,
    }


def message_to_dict(message: ChatMessage, sender: Optional[User]) -> dict:
    return {
        "id": str(message.id),
        "body": message.body,
        "imageUrl": message.image_url,
        "sender": (
            {"username": sender.username, "name": sender.name, "profilePhotoUrl": sender.image}
            if sender
            else None
        ),
        "createdAt": message.created_at.isoformat(),
    }
