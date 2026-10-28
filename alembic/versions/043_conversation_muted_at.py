"""Add conversations.participant_{one,two}_muted_at.

Revision ID: 043_conversation_muted_at
Revises: 042_post_linked_piece_ids

Mute a conversation (`ConvMenu` "Mute notifications", `NotifMessages` muted bell): per
person, per conversation, like the read times beside them. Null = not muted, so every
existing conversation stays as it is.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "043_conversation_muted_at"
down_revision: Union[str, None] = "042_post_linked_piece_ids"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("participant_one_muted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("conversations", sa.Column("participant_two_muted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "participant_two_muted_at")
    op.drop_column("conversations", "participant_one_muted_at")
