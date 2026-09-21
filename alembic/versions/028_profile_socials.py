"""Profile socials: website, instagram, twitter, category, tags.

Revision ID: 028_profile_socials
Revises: 027_auction_bids
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "028_profile_socials"
down_revision: Union[str, None] = "027_auction_bids"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("website", sa.String(500), nullable=True))
    op.add_column("users", sa.Column("instagram", sa.String(100), nullable=True))
    op.add_column("users", sa.Column("twitter", sa.String(100), nullable=True))
    op.add_column("users", sa.Column("category", sa.String(50), nullable=True))
    op.add_column("users", sa.Column("tags", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "tags")
    op.drop_column("users", "category")
    op.drop_column("users", "twitter")
    op.drop_column("users", "instagram")
    op.drop_column("users", "website")
