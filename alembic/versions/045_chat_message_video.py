"""Add chat_messages.video_url and thumbnail_url.

Revision ID: 045_chat_message_video
Revises: 044_conversation_piece_id

Video messages (`Conversation`: "Photos and videos send as media bubbles"). A message now
carries text, a photo, or a video with an optional poster frame; the check that a message
has something in it counts the video too. Existing messages are unchanged.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "045_chat_message_video"
down_revision: Union[str, None] = "044_conversation_piece_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("video_url", sa.String(1024), nullable=True))
    op.add_column("chat_messages", sa.Column("thumbnail_url", sa.String(1024), nullable=True))
    op.drop_constraint("ck_chat_messages_body_or_image", "chat_messages", type_="check")
    op.create_check_constraint(
        "ck_chat_messages_has_content",
        "chat_messages",
        "body IS NOT NULL OR image_url IS NOT NULL OR video_url IS NOT NULL",
    )


def downgrade() -> None:
    # Video-only messages can't satisfy the old check; they go with the columns.
    op.execute("DELETE FROM chat_messages WHERE body IS NULL AND image_url IS NULL")
    op.drop_constraint("ck_chat_messages_has_content", "chat_messages", type_="check")
    op.create_check_constraint(
        "ck_chat_messages_body_or_image",
        "chat_messages",
        "body IS NOT NULL OR image_url IS NOT NULL",
    )
    op.drop_column("chat_messages", "thumbnail_url")
    op.drop_column("chat_messages", "video_url")
