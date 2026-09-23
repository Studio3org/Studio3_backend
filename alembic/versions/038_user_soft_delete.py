"""Add users.deleted_at for account deletion.

Revision ID: 038_user_soft_delete
Revises: 037_clear_aliased_event_covers

A hard DELETE on users cascades onto orders (buyer_id/seller_id both ondelete="CASCADE"),
which would erase the other party's order history too — a seller's sale record vanishing
because the buyer deleted their account, or vice versa. Same reasoning pieces already follow
(soft-deleted via their own deleted_at, never hard-deleted). Account deletion anonymizes the
row and sets this instead of removing it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "038_user_soft_delete"
down_revision: Union[str, None] = "037_clear_aliased_event_covers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "deleted_at")
