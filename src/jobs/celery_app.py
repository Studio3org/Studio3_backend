"""Celery application and schedule.

Replaces the in-process APScheduler, which was only safe because Render runs a single
gunicorn worker — a second worker would have double-run every sweep. That constraint stops
being acceptable once auctions hold real money: capturing a hold twice is not a cosmetic
bug, and "we only ever run one worker" is an assumption, not a guarantee.

Deployment: one worker process and one beat process, separate from the web service. Beat
must be a single instance — two beats means every schedule fires twice.

Running a worker locally on macOS:

    celery -A src.jobs.celery_app.celery_app worker --loglevel=info --pool=solo

`--pool=solo` is needed only on macOS. Celery's default prefork pool fails there under
Python 3.13+ with "not enough values to unpack" — the forked child never receives the task
registry, because macOS no longer forks a fully-initialised interpreter. Render runs Linux
on the Python pinned in .python-version, where prefork works normally, so the deployed
command below does not use solo.
"""
import os

from celery import Celery
from celery.schedules import crontab

from src.shared.config.redis_client import redis_url
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)


def _broker_url() -> str:
    """Redis, on a database index of its own.

    Deliberately not the cache index: an eviction policy that drops keys under memory
    pressure would drop queued jobs, and the jobs here capture money.
    """
    explicit = (os.getenv("CELERY_BROKER_URL") or "").strip()
    if explicit:
        return explicit
    base = redis_url().rstrip("/")
    # Swap whatever database index is on the URL for the jobs one.
    head, _, tail = base.rpartition("/")
    if tail.isdigit():
        base = head
    return f"{base}/{os.getenv('CELERY_REDIS_DB', '1')}"


celery_app = Celery("studio3", broker=_broker_url(), backend=None)

celery_app.conf.update(
    # Results are not read anywhere; storing them would just accumulate keys in Redis.
    task_ignore_result=True,
    task_acks_late=True,
    # With acks_late, a worker that dies mid-task lets the job be redelivered. Every task
    # here re-validates its target under a row lock, so redelivery is wasteful, never wrong.
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    # A task that outruns this is stuck; let it be killed and redelivered rather than
    # holding a worker slot forever.
    task_time_limit=300,
    task_soft_time_limit=240,
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "close-expired-auctions": {
            "task": "auctions.close_expired",
            "schedule": 60.0,
            # If beat was down, run once on recovery rather than replaying every missed tick.
            "options": {"expires": 55},
        },
        "expire-winner-retry-windows": {
            "task": "auctions.expire_winner_windows",
            "schedule": 60.0,
            "options": {"expires": 55},
        },
        "refresh-expiring-holds": {
            "task": "auctions.refresh_holds",
            # Nightly: card authorisations last about a week, so a daily pass has ample
            # margin, and re-authorising more often than needed invites more declines.
            "schedule": crontab(hour=3, minute=0),
        },
        "release-abandoned-orders": {
            "task": "orders.expire_abandoned",
            # Every five minutes against a fifteen-minute window: a piece is back on sale
            # well inside twenty minutes, and the sweep is cheap when there is nothing to do.
            "schedule": 300.0,
            "options": {"expires": 290},
        },
        "expire-waitlist-offers": {
            "task": "events.expire_waitlist_offers",
            "schedule": 300.0,
            "options": {"expires": 290},
        },
        "archive-past-events": {
            "task": "events.archive_past",
            "schedule": crontab(minute=0),
        },
        "reconcile-ledger": {
            "task": "ledger.reconcile",
            "schedule": crontab(hour=4, minute=0),
        },
    },
)

# Importing registers every @celery_app.task in these modules with the worker.
celery_app.autodiscover_tasks(["src.jobs"], force=True)
from src.jobs import tasks  # noqa: E402,F401  — import for the registration side effect
