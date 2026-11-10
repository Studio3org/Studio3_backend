"""Account and settings, Phase 6: hiding a piece from your own Collection.

Revision ID: 058_collection_hidden
Revises: 057_event_people_approvals

BACKEND_CHANGES B32 ("Hide from my Collection"). One row per collector per piece they've
chosen not to show on their profile's Collection. The purchase itself is untouched; undo
deletes the row.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "058_collection_hidden"
down_revision: Union[str, None] = "057_event_people_approvals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "collection_hidden",
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("piece_id", UUID, sa.ForeignKey("pieces.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("collection_hidden")
