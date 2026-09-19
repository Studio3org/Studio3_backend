"""The correlation id itself, with no dependencies.

Split out from both the logger and the Flask middleware because each needs it and they
cannot import one another: the logger is imported by everything (including the middleware),
and the middleware is what sets the value. This module is the shared floor under both.
"""
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from logging import Filter, LogRecord

# ContextVar, not threading.local: under gevent one OS thread serves many greenlets, and a
# thread-local would let concurrent requests read each other's id.
_request_id: ContextVar[str] = ContextVar("request_id", default="-")

MAX_LENGTH = 64


def clean(value: str) -> str:
    """Strip anything that would break a log line, and cap the length."""
    return "".join(c for c in (value or "") if c.isalnum() or c in "-_:.")[:MAX_LENGTH]


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def current_request_id() -> str:
    return _request_id.get()


def set_request_id(value: str):
    """Set the id and return a token the caller must reset when its work ends.

    Returning the token matters: without a reset the value outlives the request that set it,
    so anything running afterwards in the same context inherits a stale id. Under gevent
    each request greenlet gets its own context copy so the blast radius is small, but on a
    threaded worker it would attribute one request's logs to another.
    """
    return _request_id.set(clean(value) or "-")


def reset_request_id(token) -> None:
    _request_id.reset(token)


@contextmanager
def request_id(value: str):
    """Tag a unit of work that is not an HTTP request.

    Webhook handling is the case that matters: one Stripe event touches the order, the
    ledger and the payout across three separate sessions, and without a shared id its log
    lines are indistinguishable from a concurrent delivery's.
    """
    token = _request_id.set(clean(value) or "-")
    try:
        yield
    finally:
        _request_id.reset(token)


class RequestIdFilter(Filter):
    """Puts the current id on every record so the formatter can print it."""

    def filter(self, record: LogRecord) -> bool:
        record.request_id = current_request_id()
        return True
