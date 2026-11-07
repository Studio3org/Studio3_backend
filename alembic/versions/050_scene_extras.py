"""Scenes: several photos, comments off; drafts: step and failed publish.

Revision ID: 050_scene_extras
Revises: 049_user_privacy_version

BACKEND_CHANGES B33. posts.media_urls (every photo of a photo Scene; media_url stays the
first), posts.comments_off, and on both posts and pieces draft_step (the posting step a draft
stopped on) and publish_failed (saved, then going live failed). Existing rows: one photo,
comments on, no step, not failed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "050_scene_extras"
down_revision: Union[str, None] = "049_user_privacy_version"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("media_urls", postgresql.ARRAY(sa.String(1024)), nullable=True))
    op.add_column(
        "posts",
        sa.Column("comments_off", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    for table in ("posts", "pieces"):
        op.add_column(table, sa.Column("draft_step", sa.String(32), nullable=True))
        op.add_column(
            table,
            sa.Column("publish_failed", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    for table in ("pieces", "posts"):
        op.drop_column(table, "publish_failed")
        op.drop_column(table, "draft_step")
    op.drop_column("posts", "comments_off")
    op.drop_column("posts", "media_urls")
