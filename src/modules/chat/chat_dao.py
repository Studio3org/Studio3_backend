"""Chat DAO — general-purpose 1:1 conversations."""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, or_, and_, tuple_, case
from sqlalchemy.orm import Session

from src.shared.models.block import Block
from src.shared.models.chat import Conversation, ChatMessage
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.storage.s3_service import validate_user_media_url
from src.shared.utils.app_error import AppError


def utc_now():
    return datetime.now(timezone.utc)


def _ordered_pair(user_a: uuid.UUID, user_b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (user_a, user_b) if str(user_a) < str(user_b) else (user_b, user_a)


def search_users(db: Session, viewer_id: Optional[uuid.UUID], query: str, limit: int = 20) -> list[User]:
    """Users matching `query` (username or name, case-insensitive substring) for the "new
    message" composer — excludes the viewer themself and anyone blocked in either direction.
    Doesn't filter on `message_permission`: that's enforced at send time (start_conversation),
    same as the rest of the app lets you find/view a private account before following it.

    Also backs Explore's people search, which signed-out guests can use — with no viewer
    there's nobody to exclude."""
    like = f"%{query.strip()}%"
    q = select(User).where(
        or_(User.username.ilike(like), User.name.ilike(like)),
        User.deleted_at.is_(None),
    )
    if viewer_id:
        blocked_subquery = select(Block.blocked_id).where(Block.blocker_id == viewer_id)
        blocked_by_subquery = select(Block.blocker_id).where(Block.blocked_id == viewer_id)
        q = q.where(
            User.id != viewer_id,
            User.id.not_in(blocked_subquery),
            User.id.not_in(blocked_by_subquery),
        )
    return list(db.execute(q.order_by(User.username.asc()).limit(limit)).scalars().all())


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


def _first_sender_id():
    """Correlated subquery: who sent a conversation's first message — i.e. who started it.

    A thread has no stored initiator; its opening message is the request, so its sender is
    the one waiting on a pending thread and the other participant is the one who decides.
    """
    return (
        select(ChatMessage.sender_id)
        .where(ChatMessage.conversation_id == Conversation.id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .limit(1)
        .correlate(Conversation)
        .scalar_subquery()
    )


def initiator_id(db: Session, conversation: Conversation) -> Optional[uuid.UUID]:
    return db.execute(
        select(ChatMessage.sender_id)
        .where(ChatMessage.conversation_id == conversation.id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .limit(1)
    ).scalar_one_or_none()


def _inbox_query(user_id: uuid.UUID):
    """Open threads for this user, plus pending requests they sent (still waiting on the other
    side). The recipient of a pending thread sees it in list_requests() instead — matches the
    Instagram-style "message requests" split."""
    return select(Conversation).where(
        or_(Conversation.participant_one_id == user_id, Conversation.participant_two_id == user_id),
        or_(
            Conversation.status == "open",
            and_(Conversation.status == "pending", _first_sender_id() == user_id),
        ),
    )


def _newest_first(db: Session, q, limit: int, before: Optional[tuple]) -> list[Conversation]:
    if before:
        before_ts, before_id = before
        q = q.where(tuple_(Conversation.last_message_at, Conversation.id) < tuple_(before_ts, before_id))
    return list(
        db.execute(
            q.order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(limit)
        ).scalars().all()
    )


def list_inbox(
    db: Session, user_id: uuid.UUID, limit: int = 20, before: Optional[tuple] = None
) -> list[Conversation]:
    """The Messages tab: see `_inbox_query`."""
    return _newest_first(db, _inbox_query(user_id), limit, before)


# Postgres's default LIKE escape character, so the patterns need no ESCAPE clause.
_LIKE_ESCAPE = "\\"


def contains_pattern(query: str) -> str:
    """An ILIKE pattern for `query` anywhere, with its own `%`, `_` and backslash taken literally."""
    escaped = (
        query.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def search_inbox_query(user_id: uuid.UUID, query: str):
    """The inbox's conversations whose other person's name or handle, or any message,
    contains `query`, case-insensitive ("Search messages" on `NotifMessages`, B10)."""
    like = contains_pattern(query)
    other_id = case(
        (Conversation.participant_one_id == user_id, Conversation.participant_two_id),
        else_=Conversation.participant_one_id,
    )
    by_person = (
        select(User.id)
        .where(
            User.id == other_id,
            or_(User.name.ilike(like), User.username.ilike(like)),
        )
        .exists()
    )
    by_message = (
        select(ChatMessage.id)
        .where(
            ChatMessage.conversation_id == Conversation.id,
            ChatMessage.body.ilike(like),
        )
        .exists()
    )
    return _inbox_query(user_id).where(or_(by_person, by_message))


def search_inbox(
    db: Session, user_id: uuid.UUID, query: str, limit: int = 20, before: Optional[tuple] = None
) -> list[Conversation]:
    """`search_inbox_query`, newest first and paged like the inbox."""
    return _newest_first(db, search_inbox_query(user_id, query), limit, before)


def newest_match(db: Session, conversation_id: uuid.UUID, query: str) -> Optional[ChatMessage]:
    """The newest message in the conversation whose text contains `query`, if any."""
    return db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.body.ilike(contains_pattern(query)),
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def match_preview(body: str, query: str, width: int = 140) -> str:
    """`body` cut to `width` so its first match of `query` shows on the row's one line: from
    the start when the match is near it, otherwise from a word or two before it, after "…"."""
    text = " ".join(body.split())
    at = text.lower().find(query.lower())
    lead = 24
    if at <= lead:
        return text[:width]
    start = at - lead
    space = text.find(" ", start, at)
    if space != -1:
        start = space + 1
    return "…" + text[start:start + width - 1]


def list_requests(
    db: Session, user_id: uuid.UUID, limit: int = 20, before: Optional[tuple] = None
) -> list[Conversation]:
    """Pending threads awaiting this user's accept/decline (they're the non-initiating side)."""
    q = select(Conversation).where(
        or_(Conversation.participant_one_id == user_id, Conversation.participant_two_id == user_id),
        Conversation.status == "pending",
        _first_sender_id() != user_id,
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
    video_url: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
) -> ChatMessage:
    message = ChatMessage(
        id=uuid.uuid4(), conversation_id=conversation.id, sender_id=sender_id, body=body,
        image_url=image_url, video_url=video_url, thumbnail_url=thumbnail_url,
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


def piece_for_new_chat(
    db: Session, piece_id: Optional[uuid.UUID], user_a: uuid.UUID, user_b: uuid.UUID
) -> Optional[Piece]:
    """The piece a chat is being started from (B9), if it exists, is not deleted, and belongs
    to one of the two people. Anything else is ignored: the message still goes."""
    piece = db.get(Piece, piece_id) if piece_id else None
    if piece is None or piece.deleted_at is not None or piece.user_id not in (user_a, user_b):
        return None
    return piece


def piece_summary(db: Session, conversation: Conversation) -> Optional[dict]:
    """The piece the chat started from, for the Messages thumbnail and the "About this piece"
    card: id, title, picture, price when for sale, and its artist's username (the card sits
    on the side of whoever asked). None when there is none or it was deleted."""
    if not getattr(conversation, "piece_id", None):
        return None
    piece = db.get(Piece, conversation.piece_id)
    if piece is None or piece.deleted_at is not None:
        return None
    from src.modules.notifications.notifications_dao import piece_cover_url

    artist = db.get(User, piece.user_id)
    return {
        "id": str(piece.id),
        "title": piece.title,
        "imageUrl": piece_cover_url(db, piece),
        "priceCents": piece.price_cents if piece.is_for_sale else None,
        "artistUsername": artist.username if artist else None,
    }


def is_muted_for(conversation: Conversation, user_id: uuid.UUID) -> bool:
    """Whether this person muted the conversation (B8): no push, no unread badge count."""
    if conversation.participant_one_id == user_id:
        return conversation.participant_one_muted_at is not None
    if conversation.participant_two_id == user_id:
        return conversation.participant_two_muted_at is not None
    return False


def set_muted(db: Session, conversation: Conversation, user_id: uuid.UUID, muted: bool) -> bool:
    """Mutes or unmutes the conversation for this person only; returns the new state."""
    value = utc_now() if muted else None
    if conversation.participant_one_id == user_id:
        conversation.participant_one_muted_at = value
    elif conversation.participant_two_id == user_id:
        conversation.participant_two_muted_at = value
    db.commit()
    return is_muted_for(conversation, user_id)


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
    """Count open conversations with unread messages from the other party. Muted ones are left
    out (they still show unread in the list)."""
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
        if is_muted_for(conversation, user_id):
            continue
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
    piece: Optional[dict] = None,
) -> dict:
    """`piece` is `piece_summary(...)` for a chat started from a piece (B9), else None."""
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
        "muted": is_muted_for(conversation, viewer_id),
        "piece": piece,
        "status": conversation.status,
        "otherPartyReadAt": other_read.isoformat() if other_read else None,
    }


def message_media(raw: Optional[dict]) -> dict:
    """What a message attaches, from a request body or a socket payload: a photo (`imageUrl`)
    or a video (`videoUrl`, with an optional poster frame `thumbnailUrl`), not both (B11).
    Returns `create_message`'s keyword arguments."""
    raw = raw or {}

    def url(key):
        value = raw.get(key)
        return value.strip() or None if isinstance(value, str) else None

    image_url, video_url, thumbnail_url = url("imageUrl"), url("videoUrl"), url("thumbnailUrl")
    if image_url and video_url:
        raise AppError("A message carries a photo or a video, not both.", 400)
    if thumbnail_url and not video_url:
        raise AppError("thumbnailUrl goes with a videoUrl.", 400)
    return {"image_url": image_url, "video_url": video_url, "thumbnail_url": thumbnail_url}


def check_video_is_theirs(username: str, media: dict) -> None:
    """A video and its poster must be ones the sender uploaded (`/api/media/presign`, `chat`):
    a message can't point at someone else's file or anywhere off the media store."""
    for key in ("video_url", "thumbnail_url"):
        if media.get(key):
            validate_user_media_url(username, media[key])


def message_preview(message: Optional[ChatMessage]) -> Optional[str]:
    """The Messages row's second line: the text, else "Photo" or "Video"."""
    if message is None:
        return None
    if message.body:
        return message.body[:140]
    if message.image_url:
        return "Photo"
    if getattr(message, "video_url", None):
        return "Video"
    return None


def push_text(text: Optional[str], media: Optional[dict] = None) -> str:
    """A message push's text: the message, else what it sent."""
    if text:
        return text[:100]
    return "Sent a video" if (media or {}).get("video_url") else "Sent a photo"


def message_to_dict(message: ChatMessage, sender: Optional[User]) -> dict:
    return {
        "id": str(message.id),
        "body": message.body,
        "imageUrl": message.image_url,
        "videoUrl": message.video_url,
        "thumbnailUrl": message.thumbnail_url,
        "sender": (
            {"username": sender.username, "name": sender.name, "profilePhotoUrl": sender.image}
            if sender
            else None
        ),
        "createdAt": message.created_at.isoformat(),
    }
