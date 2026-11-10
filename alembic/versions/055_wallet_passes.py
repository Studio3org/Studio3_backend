"""Wallet passes: Apple's device registrations, and when each ticket's pass last changed.

Revision ID: 055_wallet_passes
Revises: 054_event_ticketing

BACKEND_CHANGES B25. A ticket can be added to Apple Wallet and Google Wallet (Phase 3A).

* wallet_apple_registrations: Apple Wallet registers each device that holds a pass with our
  pass web service, with a push token. When a ticket changes (the event moves, the ticket is
  refunded), every registered device is pushed and asks for the new pass.
* event_tickets.pass_updated_at: what Apple's "which of my passes changed since X" asks
  about. Existing tickets start at their creation time.

Google Wallet keeps its own copy of each pass and is updated through its API, so it needs no
table here.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "055_wallet_passes"
down_revision: Union[str, None] = "054_event_ticketing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("event_tickets", sa.Column("pass_updated_at", sa.DateTime(timezone=True),
                                             nullable=True))
    op.execute("UPDATE event_tickets SET pass_updated_at = created_at")
    op.alter_column("event_tickets", "pass_updated_at", nullable=False)
    op.create_index("ix_event_tickets_pass_updated_at", "event_tickets", ["pass_updated_at"])

    op.create_table(
        "wallet_apple_registrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_library_id", sa.String(255), nullable=False, index=True),
        sa.Column("push_token", sa.String(255), nullable=False),
        sa.Column("pass_type_id", sa.String(255), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("event_tickets.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("device_library_id", "pass_type_id", "ticket_id",
                            name="uq_wallet_apple_registration"),
    )


def downgrade() -> None:
    op.drop_table("wallet_apple_registrations")
    op.drop_index("ix_event_tickets_pass_updated_at", table_name="event_tickets")
    op.drop_column("event_tickets", "pass_updated_at")
