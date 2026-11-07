"""Add users.privacy_version.

Revision ID: 049_user_privacy_version
Revises: 048_event_faqs

Which Privacy Policy an account agreed to, beside terms_accepted_at and terms_version
(BACKEND_CHANGES B30). Null for accounts that agreed before it was recorded.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "049_user_privacy_version"
down_revision: Union[str, None] = "048_event_faqs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("privacy_version", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "privacy_version")
