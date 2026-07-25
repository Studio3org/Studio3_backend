"""Add location field to pieces and posts.

Revision ID: 018_location
Revises: 017_chat
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "018_location"
down_revision: Union[str, None] = "017_chat"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("pieces", sa.Column("location", sa.String(255), nullable=True))
    op.add_column("posts", sa.Column("location", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "location")
    op.drop_column("pieces", "location")
