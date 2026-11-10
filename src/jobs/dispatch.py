"""Run a piece of work after the request: in the background where there is a worker, inline
where there isn't.

The first on-demand jobs (ticket emails, fan-out) go through here so the calling code never
cares which. Deployed environments have Celery workers, so the work is queued and the request
returns at once. Development and tests have no worker, so it runs inline — otherwise a local
run would queue emails that nothing ever sends.

`fn` must be importable by dotted path and safe to run twice (acks_late redelivers).
"""
import importlib
import os

from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

_INLINE_ENVIRONMENTS = ("development", "testing")


def _inline() -> bool:
    return os.getenv("FLASK_ENV", "development") in _INLINE_ENVIRONMENTS


def run_inline(path: str, *args) -> None:
    module_name, fn_name = path.rsplit(".", 1)
    getattr(importlib.import_module(module_name), fn_name)(*args)


def enqueue(path: str, *args) -> None:
    """Run `path(*args)` later. Never raises: a failed email must not fail a paid order."""
    try:
        if _inline():
            run_inline(path, *args)
            return
        from src.jobs.tasks import run_job

        run_job.delay(path, *[str(a) for a in args])
    except Exception:
        logger.exception("Background job %s failed to run or queue", path)
