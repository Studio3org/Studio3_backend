"""Add event_rsvps.reminded_at.

Revision ID: 046_event_rsvp_reminded_at
Revises: 045_chat_message_video

Event reminders (BACKEND_CHANGES B14): everyone going hears the day before. The column
records that they have, so the hourly job reminds each person once. Existing RSVPs start
unreminded, so going-to events in the next day are reminded on the first run.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "046_event_rsvp_reminded_at"
down_revision: Union[str, None] = "045_chat_message_video"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("event_rsvps", sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("event_rsvps", "reminded_at")
