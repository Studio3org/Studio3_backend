"""Add posts.linked_piece_ids.

Revision ID: 042_post_linked_piece_ids
Revises: 041_piece_edition_signed_mediums

A Scene can show on several pieces (ScenePhotoDetails "Linked pieces"). The ordered list
lives here; posts.linked_piece_id keeps the first so existing readers are unchanged.
Existing rows are back-filled from linked_piece_id.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "042_post_linked_piece_ids"
down_revision: Union[str, None] = "041_piece_edition_signed_mediums"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "posts",
        sa.Column("linked_piece_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True),
    )
    op.execute(
        "UPDATE posts SET linked_piece_ids = ARRAY[linked_piece_id] "
        "WHERE linked_piece_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("posts", "linked_piece_ids")
