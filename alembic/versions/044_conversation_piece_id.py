"""Add conversations.piece_id.

Revision ID: 044_conversation_piece_id
Revises: 043_conversation_muted_at

The piece a chat was started from (`Conversation` "About this piece" card, `NotifMessages`
thumbnail). Set when the first message is sent from a piece page; a deleted piece leaves it
null. Existing conversations have none.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "044_conversation_piece_id"
down_revision: Union[str, None] = "043_conversation_muted_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "piece_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pieces.id", ondelete="SET NULL", name="fk_conversations_piece_id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_constraint("fk_conversations_piece_id", "conversations", type_="foreignkey")
    op.drop_column("conversations", "piece_id")
