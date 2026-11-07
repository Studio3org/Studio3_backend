"""Wrong passwords lock password log-in for a while (BACKEND_CHANGES B30, `NoteLoginPassword`).

Five wrong passwords on one account within 15 minutes stop password log-ins to it for 15
minutes, even with the right password; the emailed login code still works and clears the count.
Counted per account in Redis, so trying from many phones adds up. If Redis can't be reached the
check is skipped (the same as the other rate limits) rather than locking everyone out.
"""
import logging

from redis.exceptions import RedisError

from src.shared.config.redis_client import get_redis_client

logger = logging.getLogger(__name__)

MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60

LOCKED_MESSAGE = (
    "Too many wrong passwords. Log in with a code sent to your email, "
    "or try your password again in 15 minutes."
)


def _fail_key(user_id) -> str:
    return f"login:fail:{user_id}"


def _lock_key(user_id) -> str:
    return f"login:lock:{user_id}"


def is_locked(user_id) -> bool:
    try:
        return bool(get_redis_client().exists(_lock_key(user_id)))
    except RedisError:
        logger.warning("login lock check skipped: Redis unavailable")
        return False


def record_failure(user_id) -> bool:
    """Counts a wrong password; True when this one locks the account."""
    try:
        r = get_redis_client()
        key = _fail_key(user_id)
        count = r.incr(key)
        if count == 1:
            r.expire(key, LOCK_SECONDS)
        if count >= MAX_FAILURES:
            r.set(_lock_key(user_id), "1", ex=LOCK_SECONDS)
            r.delete(key)
            return True
        return False
    except RedisError:
        logger.warning("login failure not counted: Redis unavailable")
        return False


def clear(user_id) -> None:
    """A good password or a login code: the count starts again and any lock lifts."""
    try:
        r = get_redis_client()
        r.delete(_fail_key(user_id), _lock_key(user_id))
    except RedisError:
        logger.warning("login lock not cleared: Redis unavailable")
