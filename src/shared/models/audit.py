"""AuditEvent — who did what, kept somewhere it survives.

Every money-moving action already writes a log line. That is not enough: on Render the
filesystem is ephemeral and the console keeps a rolling window, so "who refunded this order
and when" is answerable for about a day and then never again. An action that moves somebody
else's money needs a record with the same lifetime as the money.

Deliberately append-only. There is no update path and no delete path in the service that
writes these, because a record an operator can edit is not a record of what the operator
did.

Distinct from the ledger, which records *money*. This records *decisions* — including the
ones that moved no money, like resolving a report or cancelling an event, which the ledger
has no reason to know about.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


# --- what happened ------------------------------------------------------------------------
# Named for the decision, not the endpoint: an action reached from two places is still one
# thing that happened, and an ops person reading this wants the verb.
AUDIT_REFUND_ISSUED = "refund_issued"
AUDIT_PAYOUT_RETRIED = "payout_retried"
# Resolving a dispute deliberately has no action of its own: it always ends in a refund or a
# release, and those are recorded. A generic "dispute_resolved" beside them would say less
# while adding a filter option that never tells anyone anything.
AUDIT_PAYOUT_RELEASED = "payout_released"
AUDIT_REPORT_RESOLVED = "report_resolved"
AUDIT_SHIPMENT_CREATED = "shipment_created"
AUDIT_SHIPMENT_UPDATED = "shipment_updated"
AUDIT_AUCTION_CANCELLED = "auction_cancelled"
AUDIT_AUCTION_SETTLED = "auction_settled"
AUDIT_WINNER_CASCADED = "winner_cascaded"
AUDIT_EVENT_CANCELLED = "event_cancelled"
AUDIT_EVENT_DELETED = "event_deleted"
# Written with the real name/username still in the actor_label — captured before the
# anonymization that account deletion does to the row itself, so this is the one place an
# operator can still see who this was. The reason/feedback the user gave live in `detail`.
AUDIT_ACCOUNT_DELETED = "account_deleted"
AUDIT_ADMIN_LOGIN = "admin_login"
# Written by the nightly reconciliation when the ledger and Stripe disagree. Recorded rather
# than only logged because drift is often investigated days later, and Render's logs are gone
# by then.
AUDIT_LEDGER_DRIFT = "ledger_drift_detected"
# A refund arrived from outside the app that does not cover the whole order — a partial one,
# or one charge of an auction's two. Recorded rather than booked: apportioning it across
# commission, shipping and tax is a product decision nobody has made.
AUDIT_REFUND_NEEDS_REVIEW = "refund_needs_review"

# Ordered for the filter dropdown: the ones an operator looks for first.
AUDIT_ACTIONS = (
    AUDIT_REFUND_ISSUED,
    AUDIT_PAYOUT_RETRIED,
    AUDIT_PAYOUT_RELEASED,
    AUDIT_AUCTION_CANCELLED,
    AUDIT_WINNER_CASCADED,
    AUDIT_AUCTION_SETTLED,
    AUDIT_EVENT_CANCELLED,
    AUDIT_EVENT_DELETED,
    AUDIT_ACCOUNT_DELETED,
    AUDIT_REPORT_RESOLVED,
    AUDIT_SHIPMENT_CREATED,
    AUDIT_SHIPMENT_UPDATED,
    AUDIT_ADMIN_LOGIN,
    AUDIT_LEDGER_DRIFT,
    AUDIT_REFUND_NEEDS_REVIEW,
)

# Who or what did it. An operator is a person; the system is a scheduled job acting on its
# own, which is a materially different thing to read in a list.
ACTOR_ADMIN = "admin"
ACTOR_SYSTEM = "system"
ACTOR_USER = "user"


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        # The two questions actually asked of this table: "what happened to this order?" and
        # "what did this person do?".
        Index("ix_audit_events_subject", "subject_type", "subject_id"),
        Index("ix_audit_events_actor_created", "actor_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action = Column(String(48), nullable=False, index=True)

    # SET NULL rather than CASCADE: deleting a staff account must not erase what they did.
    actor_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_type = Column(String(16), default=ACTOR_ADMIN, server_default=ACTOR_ADMIN,
                        nullable=False)
    # Denormalised on purpose. The point of an audit row is to still make sense years later,
    # after the account has been renamed or removed.
    actor_label = Column(String(160), nullable=True)

    # What it was done to — "order", "auction", "event", "report".
    subject_type = Column(String(32), nullable=True)
    subject_id = Column(UUID(as_uuid=True), nullable=True)

    # Free-form, and read by humans rather than parsed: amounts, reasons, before/after.
    # Never anything that would make this row a second copy of someone's personal details.
    detail = Column(JSONB, nullable=True)
    note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
