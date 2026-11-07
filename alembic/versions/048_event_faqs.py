"""Add events.faqs.

Revision ID: 048_event_faqs
Revises: 047_event_city_featured

The event page's host FAQs (BACKEND_CHANGES B24): a JSON list of {"question", "answer"} in the
host's order; null for none.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "048_event_faqs"
down_revision: Union[str, None] = "047_event_city_featured"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("events", sa.Column("faqs", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("events", "faqs")
