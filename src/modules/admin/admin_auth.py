"""Session-cookie auth for the browser-facing admin UI.

Separate from the mobile JWT/Redis-session mechanism in src/middlewares/auth_middleware.py
on purpose: this is a browser tool, so it uses a signed session cookie (plus CSRF on every
form post). The JWT-based `admin_required` in auth_middleware covers API clients instead.

Authorization is the `is_admin` flag only. User.role is a marketing category
(artist/collector/enthusiast) and is never an authorization signal.
"""
import uuid
from datetime import datetime, timezone
from functools import wraps

from flask import g, redirect, session, url_for

from src.shared.config.database import SessionLocal
from src.modules.user.user_dao import get_user_by_id

SESSION_USER_KEY = "admin_user_id"
SESSION_STARTED_KEY = "admin_started_at"

# Absolute session lifetime. An ops session that has been open this long must re-auth,
# regardless of activity — these accounts can move money.
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60


def login_session(user) -> None:
    session.clear()
    session[SESSION_USER_KEY] = str(user.id)
    session[SESSION_STARTED_KEY] = datetime.now(timezone.utc).timestamp()
    session.permanent = True


def logout_session() -> None:
    session.clear()


def _session_expired() -> bool:
    started = session.get(SESSION_STARTED_KEY)
    if not started:
        return True
    age = datetime.now(timezone.utc).timestamp() - float(started)
    return age > SESSION_MAX_AGE_SECONDS


def current_admin():
    """Return the logged-in admin User, or None. Re-checks is_admin on every request so a
    revoked admin loses access immediately rather than at session expiry."""
    user_id = session.get(SESSION_USER_KEY)
    if not user_id or _session_expired():
        return None
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(user_id))
        if not user or not user.is_admin:
            return None
        db.expunge(user)
        return user
    except (ValueError, TypeError):
        return None
    finally:
        db.close()


def admin_session_required(f):
    """Gate an HTML admin route. Redirects to the login page rather than returning a JSON
    error, since the caller here is a browser, not an API client."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        admin = current_admin()
        if not admin:
            logout_session()
            return redirect(url_for("admin.login_form"))
        g.admin = admin
        return f(*args, **kwargs)
    return wrapper
