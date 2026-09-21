"""An append-only record of who did what.

Revision ID: 035_audit_events
Revises: 034_event_rsvps

Every money-moving action already writes a log line, and on Render that log is a rolling
window on an ephemeral filesystem — so "who refunded this order, and when" is answerable for
about a day and then never again. An action that moves somebody else's money deserves a
record with the same lifetime as the money.

Distinct from the ledger, which records money. This records decisions, including the ones
that moved none — resolving a report, cancelling an event — which the ledger has no reason
to know about.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "035_audit_events"
down_revision: Union[str, None] = "034_event_rsvps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_type", sa.String(16), server_default="admin", nullable=False),
        # Denormalised deliberately: an audit row has to still make sense years later, after
        # the account has been renamed or deleted.
        sa.Column("actor_label", sa.String(160), nullable=True),
        sa.Column("subject_type", sa.String(32), nullable=True),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # SET NULL, not CASCADE: removing a staff account must never erase what they did.
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_events_action"), "audit_events", ["action"], unique=False)
    op.create_index(
        op.f("ix_audit_events_created_at"), "audit_events", ["created_at"], unique=False
    )
    # The two questions actually asked of this table.
    op.create_index(
        "ix_audit_events_subject", "audit_events", ["subject_type", "subject_id"], unique=False
    )
    op.create_index(
        "ix_audit_events_actor_created", "audit_events", ["actor_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_actor_created", table_name="audit_events")
    op.drop_index("ix_audit_events_subject", table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_created_at"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_action"), table_name="audit_events")
    op.drop_table("audit_events")
