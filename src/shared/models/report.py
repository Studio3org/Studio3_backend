"""Report — a user flagging a piece, post, or another user for moderation review.

Unlike Dispute (order-specific, one per order), a target can be reported by many different
users, so there's no uniqueness constraint here; the controller instead no-ops a repeat
report from the same reporter while one is still open, to avoid duplicate spam from repeated
taps.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


REPORT_REASONS = ("spam", "stolen_work", "inappropriate", "harassment", "other")

REPORT_OPEN = "open"
REPORT_RESOLVED = "resolved"
REPORT_DISMISSED = "dismissed"


class Report(Base):
    __tablename__ = "reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reporter_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_type = Column(String(16), nullable=False)  # piece | post | user
    target_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    reason = Column(String(32), nullable=False)
    details = Column(Text, nullable=True)
    status = Column(String(16), default=REPORT_OPEN, nullable=False, index=True)
    # Resolution audit, same shape as Dispute's.
    resolved_by_admin_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    resolution_note = Column(Text, nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
