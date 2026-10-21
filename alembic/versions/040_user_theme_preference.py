"""Add users.theme_preference.

Revision ID: 040_user_theme_preference
Revises: 039_user_terms_acceptance

Stores the signed-in user's appearance choice. Null means they have not chosen,
which the client treats as light. Only "light" and "dark" are accepted by the API.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "040_user_theme_preference"
down_revision: Union[str, None] = "039_user_terms_acceptance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("theme_preference", sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "theme_preference")
