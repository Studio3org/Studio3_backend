"""Bid — an offer on an auction piece. Immutable once placed; the Piece tracks auction
state (status, auction_ends_at) so a bid row never needs to change after creation."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class Bid(Base):
    __tablename__ = "bids"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    piece_id = Column(
        UUID(as_uuid=True), ForeignKey("pieces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bidder_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount_cents = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
