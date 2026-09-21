"""Multi-image gallery for pieces: a piece_media child table, ordered by
sort_order — 0 is the cover. Existing pieces are backfilled with a single
sort_order-0 row from their current media_url/media_type/media_aspect_ratio
so the API can serve an `images` array consistently for old and new pieces
alike. `pieces.media_url`/`media_type`/`media_aspect_ratio` stay as the
denormalized cover fields every existing read path already relies on.

Revision ID: 024_piece_media_gallery
Revises: 023_series_description
"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "024_piece_media_gallery"
down_revision: Union[str, None] = "023_series_description"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    piece_media = op.create_table(
        "piece_media",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("piece_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_url", sa.String(1024), nullable=False),
        sa.Column("media_type", sa.String(32), server_default="image", nullable=False),
        sa.Column("media_aspect_ratio", sa.String(8), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["piece_id"], ["pieces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("piece_id", "sort_order", name="uq_piece_media_piece_sort"),
    )
    op.create_index(op.f("ix_piece_media_piece_id"), "piece_media", ["piece_id"], unique=False)

    connection = op.get_bind()
    existing = connection.execute(
        sa.text("SELECT id, media_url, media_type, media_aspect_ratio, created_at FROM pieces")
    ).fetchall()
    if existing:
        op.bulk_insert(
            piece_media,
            [
                {
                    "id": uuid.uuid4(),
                    "piece_id": row.id,
                    "media_url": row.media_url,
                    "media_type": row.media_type,
                    "media_aspect_ratio": row.media_aspect_ratio,
                    "sort_order": 0,
                    "created_at": row.created_at,
                }
                for row in existing
            ],
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_piece_media_piece_id"), table_name="piece_media")
    op.drop_table("piece_media")
