"""A piece's image gallery (Figma 2716:5774 cover/reorder posting flow).

One row per image, ordered by `sort_order` — `sort_order == 0` is the cover,
mirrored onto `Piece.media_url`/`media_type`/`media_aspect_ratio` so every
existing cover-only read path (feed cards, series previews, profile picks)
keeps working unchanged.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class PieceMedia(Base):
    __tablename__ = "piece_media"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    piece_id = Column(UUID(as_uuid=True), ForeignKey("pieces.id", ondelete="CASCADE"), nullable=False, index=True)
    media_url = Column(String(1024), nullable=False)
    media_type = Column(String(32), default="image", nullable=False)  # image | video
    media_aspect_ratio = Column(String(8), nullable=True)
    sort_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
