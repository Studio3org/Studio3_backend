"""Add thumbnail_url field to posts (video scene poster frame).

Revision ID: 020_thumbnail_url
Revises: 019_media_aspect_ratio
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "020_thumbnail_url"
down_revision: Union[str, None] = "019_media_aspect_ratio"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("thumbnail_url", sa.String(1024), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "thumbnail_url")
