"""Shared Flask-SocketIO instance.

Created bare here (not bound to an app) so both `src.app` (init_app) and the chat event-handler
module (which registers `@socketio.on(...)` decorators) can import the same object without a
circular import.
"""
import os

from flask_socketio import SocketIO
from socketio import RedisManager

from src.shared.config.cors import cors_allowed_origins

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Built manually (via `client_manager=`) rather than passing `message_queue=REDIS_URL`
# directly — Flask-SocketIO's automatic message_queue handling doesn't forward any extra
# options to the underlying redis client, so a `rediss://` (TLS) URL like this project's
# managed Redis would otherwise fail certificate verification on the pub/sub connection
# with no way to relax it. Mirrors the same `ssl_cert_reqs=None` relaxation already used by
# `src/shared/config/redis_client.py` for this same host.
_redis_options = {"ssl_cert_reqs": None} if REDIS_URL.startswith("rediss://") else {}
_client_manager = RedisManager(REDIS_URL, channel="studio3-chat", redis_options=_redis_options)

socketio = SocketIO(
    cors_allowed_origins=cors_allowed_origins(),
    # Fans messages out across multiple gunicorn workers/instances via Redis pub/sub.
    client_manager=_client_manager,
    async_mode="gevent",
)
