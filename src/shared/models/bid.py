"""Bid — an offer on an auction.

Amount and bidder are immutable once placed; only `status` changes, as the bid is outbid,
cancelled, wins or loses. The money lives on the Hold, and the auction's own state lives on
the Auction, so a bid row is a historical fact and nothing more.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


BID_ACTIVE = "active"
# Beaten by a higher bid. The hold behind it is released, but the row stays: the full bid
# history is the dispute record, and the seller can see it on their own listing.
BID_OUTBID = "outbid"
BID_CANCELLED = "cancelled"
BID_WON = "won"
BID_LOST = "lost"
# Won and did not complete. Deliberately not `lost`: "someone else won" and "you won and
# walked away" are different facts, and the seller needs to be able to tell them apart.
BID_FORFEITED = "forfeited"
BID_STATUSES = (BID_ACTIVE, BID_OUTBID, BID_CANCELLED, BID_WON, BID_LOST, BID_FORFEITED)


class Bid(Base):
    __tablename__ = "bids"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','outbid','cancelled','won','lost','forfeited')",
            name="ck_bids_status",
        ),
        CheckConstraint("amount_cents > 0", name="ck_bids_amount_positive"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    auction_id = Column(
        UUID(as_uuid=True), ForeignKey("auctions.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    bidder_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount_cents = Column(Integer, nullable=False)
    # Cancelled bids stay in the table: they are the dispute record for an auction that was
    # withdrawn, and deleting them would destroy the audit trail the spec requires.
    status = Column(String(16), default=BID_ACTIVE, server_default=BID_ACTIVE, nullable=False)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancellation_reason = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
