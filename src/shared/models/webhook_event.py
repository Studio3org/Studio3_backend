"""StripeWebhookEvent — audit log and idempotency guard for incoming Stripe webhooks.

Every verified event is inserted here before it is processed. The unique constraint on
stripe_event_id is what makes duplicate deliveries (Stripe retries, and does not guarantee
ordering) a no-op rather than a double-processing bug. Also satisfies the PRD's requirement
to log all webhook events for audit (FR-2.5).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class StripeWebhookEvent(Base):
    __tablename__ = "stripe_webhook_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stripe_event_id = Column(String(255), nullable=False, unique=True, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    # NOTE: contains buyer PII (name, address, email). Needs a retention/redaction policy
    # once scheduled jobs exist — see the Phase 1 plan's out-of-scope list.
    payload = Column(JSONB, nullable=False)
    # Null until handled; a non-null `error` with a null processed_at means it failed and
    # Stripe will have retried it.
    processed_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
