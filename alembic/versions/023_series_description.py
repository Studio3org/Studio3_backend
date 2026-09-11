"""Add optional series description for the series view page.

Revision ID: 023_series_description
Revises: 022_marketplace_payouts_schema
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "023_series_description"
down_revision: Union[str, None] = "022_marketplace_payouts_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("series", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("series", "description")
