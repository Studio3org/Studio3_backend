"""Redis OTP: lock (30s), resend limit (3 per 1 min), store/verify OTP (5 min TTL)."""
import functools
import hmac
import secrets
import string

from redis.exceptions import RedisError

from src.shared.config.redis_client import get_redis_client
from src.shared.utils.app_error import AppError

OTP_TTL = 60 * 5  # 5 min
LOCK_TTL = 30  # 30 s
RESEND_WINDOW = 60  # 1 min
RESEND_MAX = 3
# Wrong guesses a login or reset code survives (BACKEND_CHANGES B30): those codes sign someone
# in or change their password, so they are not left open to a million tries.
MAX_ATTEMPTS = 5
# Wrong guesses one address takes across all its codes in an hour (SEC-3). Generous for a
# person mistyping; at 30 an hour a 6-digit code takes years to guess.
FAILS_PER_HOUR = 30
FAILS_WINDOW = 60 * 60

OTP_SERVICE_UNAVAILABLE = "OTP service is temporarily unavailable. Please try again in a moment."


def _guard_redis(fn):
    """Turn a Redis connectivity failure into a clean 503 instead of an
    unhandled 500 — otherwise any transient connection hiccup crashes with
    a raw stack trace via the generic exception handler."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RedisError:
            raise AppError(OTP_SERVICE_UNAVAILABLE, 503)
    return wrapper


def _key_otp(email: str) -> str:
    return f"otp:{email}"


def _key_lock(email: str) -> str:
    return f"otp:lock:{email}"


def _key_resend(email: str) -> str:
    return f"otp:resend:{email}"


@_guard_redis
def acquire_lock(email: str) -> bool:
    """Try to set lock; return True if acquired."""
    r = get_redis_client()
    key = _key_lock(email)
    return r.set(key, "1", nx=True, ex=LOCK_TTL)


@_guard_redis
def check_resend_limit(email: str) -> bool:
    """True if can resend (under limit). Uses sliding window: incr resend key, set ex if new."""
    r = get_redis_client()
    key = _key_resend(email)
    count = r.incr(key)
    if count == 1:
        r.expire(key, RESEND_WINDOW)
    return count <= RESEND_MAX


@_guard_redis
def store_otp(email: str, otp: str) -> None:
    r = get_redis_client()
    r.setex(_key_otp(email), OTP_TTL, otp)


def _matches(stored, otp: str) -> bool:
    if stored is None or not otp:
        return False
    if isinstance(stored, bytes):
        stored = stored.decode()
    return hmac.compare_digest(str(stored), otp)


def _check(email: str, otp: str, *, consume: bool, max_attempts: int) -> bool:
    """Every code check goes through here, so every one of them is guess-limited.

    Two limits: a code dies after `max_attempts` wrong guesses, and one address takes at most
    FAILS_PER_HOUR wrong guesses in total — without the second, asking for a fresh code (three
    a minute) would buy five more guesses each time."""
    r = get_redis_client()
    key = _key_otp(email)
    attempts_key = f"otp:attempts:{email}"
    fails_key = f"otp:fails:{email}"
    if int(r.get(fails_key) or 0) >= FAILS_PER_HOUR:
        return False
    stored = r.get(key)
    if stored is None:
        return False
    if not _matches(stored, otp):
        fails = r.incr(fails_key)
        if fails == 1:
            r.expire(fails_key, FAILS_WINDOW)
        count = r.incr(attempts_key)
        if count == 1:
            r.expire(attempts_key, OTP_TTL)
        if count >= max_attempts:
            r.delete(key, attempts_key)
        return False
    if consume:
        r.delete(key, attempts_key)
    else:
        # Sign-up's check-only step: the remaining steps (username, phone, password) should
        # not race the original five minutes.
        r.expire(key, OTP_TTL)
    return True


@_guard_redis
def verify_otp(email: str, otp: str, max_attempts: int = MAX_ATTEMPTS) -> bool:
    """Single-use check: deleted on a match, thrown away after `max_attempts` wrong guesses.
    Used by register() and confirm_email_change()."""
    return _check(email, otp, consume=True, max_attempts=max_attempts)


@_guard_redis
def verify_otp_limited(email: str, otp: str, max_attempts: int = MAX_ATTEMPTS) -> bool:
    """Login and reset codes. Same as verify_otp; kept as its own name for those callers."""
    return _check(email, otp, consume=True, max_attempts=max_attempts)


@_guard_redis
def peek_otp(email: str, otp: str, max_attempts: int = MAX_ATTEMPTS) -> bool:
    """Check the code without spending it (sign-up's /otp/verify step); register() spends it.
    Wrong guesses count against the code exactly as they do for verify_otp."""
    return _check(email, otp, consume=False, max_attempts=max_attempts)


def generate_otp(length: int = 6) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(length))
