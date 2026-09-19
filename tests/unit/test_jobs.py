"""The job runner's wiring.

Cheap to check and expensive to get wrong: a task that is scheduled but not registered, or
registered under a name the schedule doesn't use, fails silently in production — beat logs
that it sent something and no worker ever picks it up.
"""
from src.jobs.celery_app import celery_app


def _own_tasks() -> set[str]:
    return {name for name in celery_app.tasks if not name.startswith("celery.")}


def test_every_scheduled_task_is_registered():
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    missing = scheduled - _own_tasks()
    assert not missing, (
        f"beat schedules tasks no worker can run: {sorted(missing)}. "
        "Beat would dispatch them and nothing would happen."
    )


def test_every_registered_task_is_scheduled_or_deliberately_manual():
    """Catches the reverse: a task written but never wired into the schedule."""
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    unscheduled = _own_tasks() - scheduled
    assert not unscheduled, f"registered but never scheduled: {sorted(unscheduled)}"


def test_broker_is_not_the_cache_database():
    """Jobs capture money. Sharing Redis with the cache means an eviction policy under memory
    pressure can drop a queued capture."""
    from src.shared.config.redis_client import redis_url

    assert celery_app.conf.broker_url != redis_url()
    assert celery_app.conf.broker_url.rstrip("/").split("/")[-1] == "1"


def test_redelivery_is_safe_by_configuration():
    """acks_late plus reject_on_worker_lost means a task killed mid-run is redelivered rather
    than lost. Every task re-validates under a row lock, so redelivery is wasteful, not wrong.
    """
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_frequent_schedules_expire_before_the_next_tick():
    """Without an expiry, a worker coming back after an outage replays every missed tick."""
    for name, entry in celery_app.conf.beat_schedule.items():
        schedule = entry["schedule"]
        if isinstance(schedule, (int, float)) and schedule <= 300:
            expires = entry.get("options", {}).get("expires")
            assert expires is not None and expires < schedule, name


def test_the_in_process_scheduler_is_retired():
    """start_scheduler must be a no-op: a background thread in the web process would run the
    auction sweep alongside the worker, and double-close auctions."""
    import threading

    from src.shared.scheduler import start_scheduler

    before = threading.active_count()
    start_scheduler()
    assert threading.active_count() == before
