"""Request correlation IDs.

Every log line carries the id of the request (or the Stripe event, or the scheduler tick)
that produced it, so an incident can be reconstructed by grepping one value instead of
guessing which interleaved lines belong together. With a single gunicorn worker serving
concurrent greenlets, interleaving is the normal case, not the exception.
"""
import time

from flask import Flask, g, request

from src.shared.utils.logger import get_logger
from src.shared.utils.request_id import (
    clean,
    new_request_id,
    reset_request_id,
    set_request_id,
)

logger = get_logger(__name__)


def init_request_context(app: Flask) -> None:
    """Assign an id per request and emit one access line when it finishes.

    Register before the error handler, so a 500's traceback carries the same id as the
    access line that precedes it.
    """

    @app.before_request
    def _assign_request_id():
        # A caller-supplied id is honoured so a mobile client can correlate its own
        # logs with ours, after being stripped and length-capped.
        incoming = clean(request.headers.get("X-Request-ID", ""))
        g.request_id = incoming or new_request_id()
        g.request_started_at = time.monotonic()
        g.request_id_token = set_request_id(g.request_id)

    @app.after_request
    def _emit_access_log(response):
        started = getattr(g, "request_started_at", None)
        duration_ms = round((time.monotonic() - started) * 1000, 1) if started else -1
        rid = getattr(g, "request_id", "-")
        response.headers["X-Request-ID"] = rid
        user = getattr(g, "user", None)
        # Gunicorn's own access log is not enabled on this deployment, so this is the only
        # record that a request happened at all.
        logger.info(
            "%s %s -> %s (%.1fms) user=%s",
            request.method,
            request.path,
            response.status_code,
            duration_ms,
            (user or {}).get("id", "-"),
        )
        return response

    @app.teardown_request
    def _clear_request_id(_exc=None):
        token = g.pop("request_id_token", None)
        if token is not None:
            reset_request_id(token)
