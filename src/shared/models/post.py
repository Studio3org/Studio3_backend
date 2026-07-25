"""Post (process/WIP) model."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Boolean, Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID

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
    is_process = Column(Boolean, default=True, nullable=False)
    linked_piece_id = Column(UUID(as_uuid=True), ForeignKey("pieces.id", ondelete="SET NULL"), nullable=True, index=True)
    status = Column(String(32), default="live", nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
