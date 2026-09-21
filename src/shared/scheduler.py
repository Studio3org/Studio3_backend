"""Deprecated — scheduled work now runs on Celery.

Kept as a pointer rather than deleted so anyone following an old reference lands here
instead of wondering where the auction closer went. See src/jobs/celery_app.py.

The in-process APScheduler this replaced was only safe because Render runs gunicorn with
`-w 1`; a second worker would have double-run every sweep. That was tolerable when the one
job flipped a status, and is not once the same sweep captures money.
"""
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)


def start_scheduler() -> None:
    """No-op. Scheduled work runs in the Celery worker, not the web process."""
    logger.info("In-process scheduler retired; scheduled work runs on Celery (src/jobs).")
