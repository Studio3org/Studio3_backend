"""Scheduled work.

Each task is a thin wrapper: it tags a correlation id, catches and logs, and delegates to a
module that owns the actual logic. Keeping the logic out of here means it stays callable and
testable without a broker.

Every task must be safe to run twice. `acks_late` plus a worker dying mid-job means
redelivery is normal, not exceptional — so each one re-reads and re-validates its target
under a row lock rather than trusting whatever selected it.
"""
import uuid

from src.jobs.celery_app import celery_app
from src.shared.utils.logger import get_logger
from src.shared.utils.request_id import request_id

logger = get_logger(__name__)


def _run(name: str, fn) -> None:
    """Tag, run, and swallow. A scheduled job has no caller to return an error to, so a
    failure is logged and the next tick tries again rather than killing the worker."""
    with request_id(f"{name}:{uuid.uuid4().hex[:8]}"):
        try:
            fn()
        except Exception:
            logger.exception("Scheduled task %s failed", name)


@celery_app.task(name="auctions.close_expired")
def close_expired_auctions() -> None:
    """Close auctions past their end time: capture the winner, release everyone else."""
    from src.modules.bids.auction_closer import close_expired_auctions as run

    _run("auction-close", run)


@celery_app.task(name="auctions.expire_winner_windows")
def expire_winner_windows() -> None:
    """Pass a piece to the next bidder when the winner's window to fix payment has run out."""
    from src.modules.bids.auction_closer import expire_winner_windows as run

    _run("winner-window", run)


@celery_app.task(name="auctions.refresh_holds")
def refresh_holds() -> None:
    """Re-authorise holds approaching their capture deadline.

    Standalone auctions only — an event auction's window is hours, nowhere near a card
    authorisation's lifetime, so none of its holds are ever selected.
    """
    from src.modules.bids.hold_refresh import refresh_expiring_holds as run

    _run("hold-refresh", run)


@celery_app.task(name="orders.expire_abandoned")
def expire_abandoned_orders() -> None:
    """Put artwork back on sale when a checkout was started and never paid for.

    Nothing else does this: every other release is triggered by a Stripe webhook, and a
    collector who closes the payment sheet produces no webhook at all.
    """
    from src.modules.orders.stale_orders import expire_abandoned_orders as run

    _run("abandoned-orders", run)


@celery_app.task(name="events.expire_waitlist_offers")
def expire_waitlist_offers() -> None:
    """Pass an unclaimed waitlist spot to the next person. Placeholder until Phase 7."""
    _run("waitlist-expiry", lambda: logger.debug("Waitlist expiry: not yet implemented."))


@celery_app.task(name="events.archive_past")
def archive_past_events() -> None:
    """Archive events whose date has passed and release their held host payouts.

    Placeholder until Phase 7.
    """
    _run("event-archive", lambda: logger.debug("Event archiving: not yet implemented."))


@celery_app.task(name="ledger.reconcile")
def reconcile_ledger() -> None:
    """Compare what the books say the platform is holding against what Stripe says.

    Reports; never corrects. An automatic correction would paper over the bug that caused the
    drift and destroy the evidence of it.
    """
    from src.modules.admin.reconciliation import reconcile

    _run("ledger-reconcile", reconcile)
