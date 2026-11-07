"""Post (process/WIP) model."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Boolean, Text, ForeignKey
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class Post(Base):
    __tablename__ = "posts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    media_url = Column(String(1024), nullable=False)
    media_type = Column(String(32), nullable=False)
    caption = Column(Text, nullable=True)
    location = Column(String(255), nullable=True)
    media_aspect_ratio = Column(String(8), nullable=True)
    thumbnail_url = Column(String(1024), nullable=True)
    is_process = Column(Boolean, default=True, nullable=False)
    linked_piece_id = Column(UUID(as_uuid=True), ForeignKey("pieces.id", ondelete="SET NULL"), nullable=True, index=True)
    # Every piece the Scene shows on, in order; `linked_piece_id` stays the first so its
    # readers are unchanged. No foreign key on an array: a deleted piece is skipped on read.
    linked_piece_ids = Column(ARRAY(UUID(as_uuid=True)), nullable=True)
    # Every photo of a photo Scene, in order (BACKEND_CHANGES B33); `media_url` stays the
    # first. Null for a one-photo Scene and for video.
    media_urls = Column(ARRAY(String(1024)), nullable=True)
    # The artist turned comments off from the Scene's More menu (B33).
    comments_off = Column(Boolean, default=False, nullable=False, server_default="false")
    # A draft's posting step and whether going live failed (B33, `Drafts`).
    draft_step = Column(String(32), nullable=True)
    publish_failed = Column(Boolean, default=False, nullable=False, server_default="false")
    status = Column(String(32), default="live", nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
