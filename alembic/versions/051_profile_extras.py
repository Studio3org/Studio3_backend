"""Profile: mediums under the username, and a private Collection.

Revision ID: 051_profile_extras
Revises: 050_scene_extras

BACKEND_CHANGES B31 (the mediums part) and B34 (Private Collection). users.profile_mediums (up
to three, in order) and users.collection_private (off). Existing rows: no mediums, Collection
public.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "051_profile_extras"
down_revision: Union[str, None] = "050_scene_extras"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("profile_mediums", postgresql.JSONB(), nullable=True))
    op.add_column(
        "users",
        sa.Column("collection_private", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("users", "collection_private")
    op.drop_column("users", "profile_mediums")
