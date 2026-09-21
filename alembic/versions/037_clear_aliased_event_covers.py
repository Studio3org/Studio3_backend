"""Drop event covers that are really the host's profile banner.

Revision ID: 037_clear_aliased_event_covers
Revises: 036_index_stripe_account_id

The event create flow uploaded its flyer with the `cover` purpose, which is the *profile*
banner and resolves to one fixed key per user (`<username>/cover/banner.<ext>`). So every
published event both overwrote its host's profile banner and stored that banner's URL as its
own cover — one S3 object serving two unrelated things, and a host publishing a second event
silently changed the first one's image.

The purpose is fixed going forward. These rows cannot be: the bytes behind that key are
whichever image was uploaded last, and there is no record of which event each belonged to.
Clearing them shows an empty banner the host can fix, rather than confidently showing someone
the wrong picture.

Irreversible by nature — the downgrade cannot invent URLs it deliberately discarded.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "037_clear_aliased_event_covers"
down_revision: Union[str, None] = "036_index_stripe_account_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Matches the profile-banner key shape only. A legitimate event cover lives under
    # /events/<uuid>/original.<ext> and is untouched.
    op.execute(
        """
        UPDATE events
           SET cover_media_url = NULL
         WHERE cover_media_url LIKE '%/cover/banner.%'
        """
    )


def downgrade() -> None:
    # Nothing to restore: the URLs pointed at an object that was never the event's own.
    pass
