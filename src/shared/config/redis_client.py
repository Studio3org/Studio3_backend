"""Single Redis connection client."""
import os
from typing import Optional

import redis

_DEFAULT_URL = "redis://localhost:6379/0"
_client: Optional[redis.Redis] = None


def redis_url() -> str:
    """Read REDIS_URL on demand rather than at import time.

    Reading it at import made the value depend on whether this module was imported before
    or after dotenv ran. That holds for run.py/wsgi.py, which load the environment first,
    but not for anything that sets the environment itself (tests, one-off scripts), where
    the constant silently captured the default. Mirrors get_database_url() next door.
    """
    return os.getenv("REDIS_URL", _DEFAULT_URL)


def get_redis_client() -> redis.Redis:
    """Return the global Redis client (create if needed)."""
    global _client
    if _client is None:
        url = redis_url()
        # rediss:// = TLS; many hosted Redis (Render, Upstash) need cert verification relaxed
        kwargs = {"decode_responses": True}
        if url.startswith("rediss://"):
            kwargs["ssl_cert_reqs"] = None
        _client = redis.from_url(url, **kwargs)
    return _client


def close_redis() -> None:
    """Close the Redis connection (call on shutdown)."""
    global _client
    if _client is not None:
        _client.close()
        _client = None


def check_redis_connection() -> bool:
    """Ping Redis to verify it's reachable."""
    try:
        r = get_redis_client()
        return r.ping()
    except Exception as e:
        from src.shared.utils.logger import get_logger
        get_logger("redis").error("Redis connection failed: %s", e)
        return False
