"""In-process scheduler — the auction-closing job. render.yaml runs gunicorn with `-w 1`
(one worker), so a single in-process scheduler can't double-run this; if the deployment
ever moves to multiple workers, this would need a Redis leader lock instead."""
from apscheduler.schedulers.background import BackgroundScheduler

from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return

    from src.modules.bids.auction_closer import close_expired_auctions

    def _tick():
        try:
            close_expired_auctions()
        except Exception:
            logger.exception("close_expired_auctions tick failed")

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(_tick, "interval", minutes=1, id="close_expired_auctions")
    scheduler.start()
    _scheduler = scheduler
    logger.info("Scheduler started (close_expired_auctions every 1 minute)")
