"""Add pieces.edition, pieces.signed and pieces.mediums.

Revision ID: 041_piece_edition_signed_mediums
Revises: 040_user_theme_preference

The posting flow's Additional details (Edition, Signed) and the multi-select Medium sheet.
`medium` keeps the first medium picked, so Explore filters and every reader of it are
unchanged; `mediums` holds the full ordered list.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "041_piece_edition_signed_mediums"
down_revision: Union[str, None] = "040_user_theme_preference"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("pieces", sa.Column("edition", sa.String(32), nullable=True))
    op.add_column("pieces", sa.Column("signed", sa.Boolean(), nullable=True))
    op.add_column("pieces", sa.Column("mediums", postgresql.ARRAY(sa.String()), nullable=True))


def downgrade() -> None:
    op.drop_column("pieces", "mediums")
    op.drop_column("pieces", "signed")
    op.drop_column("pieces", "edition")
