"""Payout — the Stripe Transfer that pays an artist, held until delivery is confirmed.

Split of responsibility: the ledger tracks *what is owed* to an artist (seller_payable
balance); this table tracks *the Stripe Transfer that paid it* and its own state machine,
which is deliberately independent of the order's status (PRD 6.2).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


PAYOUT_PENDING = "pending"
PAYOUT_READY_TO_RELEASE = "ready_to_release"
PAYOUT_RELEASED = "released"
PAYOUT_TRANSFER_FAILED = "transfer_failed"


class Payout(Base):
    __tablename__ = "payouts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # One payout per order — the unique constraint is itself a duplicate-payout guard.
    order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    seller_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Set once the payout's ledger transaction is booked (i.e. after a successful transfer).
    ledger_transaction_id = Column(
        UUID(as_uuid=True),
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status = Column(String(24), default=PAYOUT_PENDING, nullable=False, index=True)
    stripe_transfer_id = Column(String(255), nullable=True, unique=True)
    # Deterministic ("payout:<order_id>"). Note Stripe only dedups on this for 24h, so it is
    # a short-window safety net — the real guards are this table's status + row locking.
    idempotency_key = Column(String(255), nullable=False, unique=True)
    failure_reason = Column(Text, nullable=True)
    released_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
