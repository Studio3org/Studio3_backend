"""Piece listing type: fixed price vs auction, plus auction duration.

Revision ID: 025_piece_listing_type
Revises: 024_piece_media_gallery
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "025_piece_listing_type"
down_revision: Union[str, None] = "024_piece_media_gallery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pieces",
        sa.Column("listing_type", sa.String(16), nullable=True),
    )
    op.add_column(
        "pieces",
        sa.Column("auction_duration_days", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pieces", "auction_duration_days")
    op.drop_column("pieces", "listing_type")
