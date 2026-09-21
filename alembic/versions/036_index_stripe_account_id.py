"""Index the connected-account id the Connect webhook looks up by.

Revision ID: 036_index_stripe_account_id
Revises: 035_audit_events

`account.updated` arrives for every change Stripe makes to an artist's connected account, and
the handler's first act is to find the owning user by `stripe_account_id`. Migration 022 added
the column without an index, so that lookup has always been a sequential scan of `users` —
cheap today, and steadily less so.

Unique as well as indexed: two users sharing one connected account would mean paying the wrong
artist, and there is no legitimate way for it to happen.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "036_index_stripe_account_id"
down_revision: Union[str, None] = "035_audit_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_users_stripe_account_id"),
        "users",
        ["stripe_account_id"],
        unique=True,
        # NULL is the normal state for anyone who has not started payout onboarding, and
        # Postgres does not treat NULLs as equal — so the unique constraint applies only to
        # rows that actually hold an account.
        postgresql_where="stripe_account_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_users_stripe_account_id"), table_name="users")
