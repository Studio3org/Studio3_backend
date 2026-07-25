"""Composite indexes for like/save target lookups.

Revision ID: 021_social_engagement_indexes
Revises: 020_thumbnail_url
"""
from typing import Sequence, Union

from alembic import op

revision: str = "021_social_engagement_indexes"
down_revision: Union[str, None] = "020_thumbnail_url"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_likes_target_type_target_id",
        "likes",
        ["target_type", "target_id"],
        unique=False,
    )
    op.create_index(
        "ix_saves_target_type_target_id",
        "saves",
        ["target_type", "target_id"],
        unique=False,
    )
    op.create_index(
        "ix_saves_user_id_target",
        "saves",
        ["user_id", "target_type", "target_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_saves_user_id_target", table_name="saves")
    op.drop_index("ix_saves_target_type_target_id", table_name="saves")
    op.drop_index("ix_likes_target_type_target_id", table_name="likes")
