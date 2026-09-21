"""Dispute — a collector-reported problem (damaged, wrong item, not received).

Distinct from a Stripe card chargeback (`charge.dispute.created`), which is the buyer's bank
clawing money back and is tracked on the order itself. This is our own in-app report, resolved
manually by an admin as either a refund or a payout release (PRD 4.7).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


DISPUTE_OPEN = "open"
DISPUTE_RESOLVED_REFUND = "resolved_refund"
DISPUTE_RESOLVED_RELEASE = "resolved_release"


class Dispute(Base):
    __tablename__ = "disputes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    opened_by_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reason = Column(Text, nullable=False)
    status = Column(String(24), default=DISPUTE_OPEN, nullable=False, index=True)
    # Resolution audit (FR-7.5): who resolved it, why, and when.
    resolved_by_admin_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    resolution_note = Column(Text, nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
