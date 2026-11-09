"""Redis-backed rate limiting."""

from __future__ import annotations

from flask import request

from src.shared.config.redis_client import get_redis_client
from src.shared.utils.app_error import AppError


def _client_ip() -> str:
    # ProxyFix (src/app.py) has already replaced remote_addr with the client address taken
    # from the trusted end of X-Forwarded-For. Reading the header here would read the
    # client-supplied end of it, which is the bug this replaced (AUTH-1).
    return request.remote_addr or "unknown"


def rate_limit(key: str, limit: int, window_seconds: int, *, fail_closed: bool = False) -> None:
    """Increment counter; raise 429 if over limit.

    With Redis down the limit cannot be counted. Most endpoints carry on (fail open); the ones
    guarding credentials pass `fail_closed` and answer 503 instead, rather than becoming
    unlimited for exactly as long as Redis is out."""
    try:
        redis_client = get_redis_client()
        full_key = f"rl:{key}"
        count = redis_client.incr(full_key)
        if count == 1:
            redis_client.expire(full_key, window_seconds)
        if count > limit:
            raise AppError("Too many requests. Please try again later.", 429)
    except AppError:
        raise
    except Exception:
        if fail_closed:
            raise AppError("Sign-in is temporarily unavailable. Please try again shortly.", 503)


def rate_limit_ip(
    action: str, limit: int, window_seconds: int = 60, *, fail_closed: bool = False
) -> None:
    rate_limit(f"{action}:ip:{_client_ip()}", limit, window_seconds, fail_closed=fail_closed)


def rate_limit_user(action: str, user_id: str, limit: int, window_seconds: int = 60) -> None:
    rate_limit(f"{action}:user:{user_id}", limit, window_seconds)
