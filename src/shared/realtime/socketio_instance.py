"""Shared Flask-SocketIO instance.

Created bare here (not bound to an app) so both `src.app` (init_app) and the chat event-handler
module (which registers `@socketio.on(...)` decorators) can import the same object without a
circular import.

Nothing in this module reads the environment at import time. The options that need it are
resolved by `socketio_options()`, which `create_app` calls when it binds the app — by which
point dotenv has run. Building the Redis client manager at import made the pub/sub backend
depend on import order, which is wrong for any entrypoint that sets its own environment.
"""
from flask_socketio import SocketIO
from socketio import RedisManager

from src.shared.config.cors import cors_allowed_origins
from src.shared.config.redis_client import redis_url

socketio = SocketIO()


def socketio_options() -> dict:
    """Server options for `socketio.init_app`, resolved at app-init time.

    The client manager is built manually (via `client_manager=`) rather than passing
    `message_queue=` directly — Flask-SocketIO's automatic message_queue handling doesn't
    forward extra options to the underlying redis client, so a `rediss://` (TLS) URL like
    this project's managed Redis would fail certificate verification on the pub/sub
    connection with no way to relax it. Mirrors the same `ssl_cert_reqs=None` relaxation
    used by `src/shared/config/redis_client.py` for this same host.
    """
    url = redis_url()
    redis_options = {"ssl_cert_reqs": None} if url.startswith("rediss://") else {}
    return {
        "cors_allowed_origins": cors_allowed_origins(),
        # Fans messages out across multiple gunicorn workers/instances via Redis pub/sub.
        "client_manager": RedisManager(url, channel="studio3-chat", redis_options=redis_options),
        "async_mode": "gevent",
    }
