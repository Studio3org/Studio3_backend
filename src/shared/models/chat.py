"""General-purpose 1:1 direct-message conversations (Instagram-style chat)."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Text, ForeignKey, UniqueConstraint, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Always stored with participant_one_id < participant_two_id (compared as strings) so a
    # lookup for a given pair of users is deterministic and the unique constraint prevents
    # duplicate threads between the same two people.
    participant_one_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    participant_two_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(16), default="open", nullable=False)  # open|pending|closed
    # "Unread for me" = last_message_at > my_read_at (or my_read_at IS NULL). Fine for a
    # strictly-2-participant thread; group chat would need a separate read-receipts table.
    participant_one_read_at = Column(DateTime(timezone=True), nullable=True)
    participant_two_read_at = Column(DateTime(timezone=True), nullable=True)
    # Muted for that person (null = not muted; `ConvMenu` "Mute notifications"): messages
    # still arrive, but no push and no count in the unread badge (migration 043).
    participant_one_muted_at = Column(DateTime(timezone=True), nullable=True)
    participant_two_muted_at = Column(DateTime(timezone=True), nullable=True)
    # The piece the chat was started from (`Conversation` "About this piece" card, the piece's
    # thumbnail in `NotifMessages`); null for a chat started anywhere else (migration 044).
    piece_id = Column(UUID(as_uuid=True), ForeignKey("pieces.id", ondelete="SET NULL"), nullable=True)
    last_message_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        UniqueConstraint("participant_one_id", "participant_two_id", name="uq_conversations_participants"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    body = Column(Text, nullable=True)
    image_url = Column(String(1024), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        CheckConstraint("body IS NOT NULL OR image_url IS NOT NULL", name="ck_chat_messages_body_or_image"),
    )
