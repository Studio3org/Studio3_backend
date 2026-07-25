"""Add media_aspect_ratio field to pieces and posts.

Revision ID: 019_media_aspect_ratio
Revises: 018_location
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "019_media_aspect_ratio"
down_revision: Union[str, None] = "018_location"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("pieces", sa.Column("media_aspect_ratio", sa.String(8), nullable=True))
    op.add_column("posts", sa.Column("media_aspect_ratio", sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "media_aspect_ratio")
    op.drop_column("pieces", "media_aspect_ratio")
