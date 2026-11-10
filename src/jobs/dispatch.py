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


def enqueue_after_commit(db, path: str, *args) -> None:
    """enqueue(path, *args) once `db`'s transaction commits — never before, and not at all if
    it rolls back.

    Work that reads what the caller just wrote must not start until it is committed: inline it
    would read the old rows, and a fast worker could too. Wallet pass updates hit exactly this:
    queued mid-transaction, they pushed "ACTIVE" for a ticket being voided.

    A SAVEPOINT committing (ledger_service.get_seller_account uses one) is not the commit:
    the caller's rows and locks are still open, and running then deadlocked a courier booking
    on the order's own row lock. Jobs wait in `db.info` for the outermost commit.
    """
    from sqlalchemy import event

    db.info.setdefault(_PENDING, []).append((path, args))
    if db.info.get(_HOOKED):
        return
    db.info[_HOOKED] = True
    event.listen(db, "after_commit", _run_pending)
    event.listen(db, "after_soft_rollback", _drop_pending)


_PENDING = "_after_commit_jobs"
_HOOKED = "_after_commit_hooked"


def _run_pending(session) -> None:
    if session.in_nested_transaction():
        return
    for path, args in session.info.pop(_PENDING, []):
        enqueue(path, *args)


def _drop_pending(session, previous_transaction) -> None:
    if previous_transaction.parent is None and not previous_transaction.nested:
        session.info.pop(_PENDING, None)
