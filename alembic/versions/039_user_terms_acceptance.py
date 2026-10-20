"""Add users.terms_accepted_at / users.terms_version.

Revision ID: 039_user_terms_acceptance
Revises: 038_user_soft_delete

App Store guideline 1.2: users must agree to terms (EULA) with a zero-tolerance clause for
objectionable content and abusive users before they can post. Recording which version each
account agreed to — and when — lets the client re-prompt existing accounts (signed up before
the terms existed) and any account whenever the terms are revised.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "039_user_terms_acceptance"
down_revision: Union[str, None] = "038_user_soft_delete"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("terms_version", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "terms_version")
    op.drop_column("users", "terms_accepted_at")
