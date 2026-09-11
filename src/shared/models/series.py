"""Series (grouping of an artist's pieces) model."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Integer, ForeignKey, UniqueConstraint, Text
from sqlalchemy.dialects.postgresql import UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class Series(Base):
    __tablename__ = "series"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class SeriesPiece(Base):
    """Join table: piece membership + ordering within a series."""
    __tablename__ = "series_pieces"
    __table_args__ = (UniqueConstraint("series_id", "piece_id", name="uq_series_piece"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    series_id = Column(UUID(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True)
    piece_id = Column(UUID(as_uuid=True), ForeignKey("pieces.id", ondelete="CASCADE"), nullable=False, index=True)
    position = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
