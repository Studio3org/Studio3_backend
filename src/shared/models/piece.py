"""Piece (finished art) model."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Boolean, Integer, Float, Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class Piece(Base):
    __tablename__ = "pieces"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    media_url = Column(String(1024), nullable=False)
    media_type = Column(String(32), nullable=False)  # image | video
    caption = Column(Text, nullable=True)
    medium = Column(String(100), nullable=True)
    materials = Column(ARRAY(String), nullable=True)
    style_tags = Column(ARRAY(String), nullable=True)
    ai_disclosed = Column(Boolean, default=False, nullable=False)
    alt_text = Column(Text, nullable=True)
    is_for_sale = Column(Boolean, default=False, nullable=False)
    # 'fixed' | 'auction' — only meaningful when is_for_sale is true.
    listing_type = Column(String(16), nullable=True)
    # Auction window in days (3–14). Starting bid is stored in price_cents.
    auction_duration_days = Column(Integer, nullable=True)
    price_cents = Column(Integer, nullable=True)
    currency = Column(String(3), default="USD", nullable=False)
    dimensions = Column(JSONB, nullable=True)  # the artwork's own size, for display
    shipping_region = Column(String(64), nullable=True)
    # Courier-facing shipping attributes. Deliberately separate from `dimensions`: couriers
    # price on the *packaged* size (crating/framing adds bulk) plus weight, and need a
    # declared value for customs/insurance. Required once is_for_sale is true.
    weight_kg = Column(Float, nullable=True)
    package_length_cm = Column(Float, nullable=True)
    package_width_cm = Column(Float, nullable=True)
    package_height_cm = Column(Float, nullable=True)
    declared_value_cents = Column(Integer, nullable=True)
    location = Column(String(255), nullable=True)
    media_aspect_ratio = Column(String(8), nullable=True)
    year_created = Column(Integer, nullable=True)
    framing_mounting = Column(Text, nullable=True)
    provenance = Column(Text, nullable=True)
    handling_notes = Column(Text, nullable=True)
    status = Column(String(32), default="live", nullable=False)  # draft|live|sold|delisted|reserved
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
