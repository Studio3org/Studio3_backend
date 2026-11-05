"""Add events.city and events.featured_at.

Revision ID: 047_event_city_featured
Revises: 046_event_rsvp_reminded_at

The Events tab (BACKEND_CHANGES B23): "Near me" picks a city and Featured is a team pick.
`city` is read from the address when an event is saved (the part before the last, without a
postcode, as the app read it); existing events are filled the same way here. `featured_at`
is set from the admin Events page; null means not featured.
"""
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "047_event_city_featured"
down_revision: Union[str, None] = "046_event_rsvp_reminded_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _city(address):
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if len(parts) < 2:
        return None
    city = parts[-2]
    return None if re.search(r"\d", city) else city[:120]


def upgrade() -> None:
    op.add_column("events", sa.Column("city", sa.String(120), nullable=True))
    op.add_column("events", sa.Column("featured_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_events_city", "events", ["city"])
    op.create_index("ix_events_featured_at", "events", ["featured_at"])

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, address FROM events WHERE address IS NOT NULL")).all()
    for event_id, address in rows:
        city = _city(address)
        if city:
            conn.execute(
                sa.text("UPDATE events SET city = :city WHERE id = :id"),
                {"city": city, "id": event_id},
            )


def downgrade() -> None:
    op.drop_index("ix_events_featured_at", table_name="events")
    op.drop_index("ix_events_city", table_name="events")
    op.drop_column("events", "featured_at")
    op.drop_column("events", "city")
