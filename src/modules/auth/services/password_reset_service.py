"""Password reset: create token in DB, send email with link."""
import hashlib
import secrets
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from src.shared.notification.email_service import send_email
from src.shared.templates.password_reset_template import get_password_reset_html
from src.modules.auth.auth_dao import (
    find_user_by_email,
    create_password_reset_token,
    find_password_reset_by_token_hash,
    delete_password_reset_tokens_for_user,
    update_user_password,
)
from src.modules.sessions.refresh_token_dao import revoke_all_for_user
from src.modules.sessions.session_service import delete_all_sessions_for_user

import os

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
RESET_TOKEN_EXPIRY_HOURS = 1
# A reset code from the app (BACKEND_CHANGES B30) buys this long to set the new password.
RESET_GRANT_MINUTES = 10


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def request_password_reset(db: Session, email: str) -> None:
    """If user exists, create token, send email. Always return same to avoid leaking."""
    user = find_user_by_email(db, email)
    if not user:
        return
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=RESET_TOKEN_EXPIRY_HOURS)
    create_password_reset_token(db, user.id, token_hash, expires_at)
    reset_url = f"{FRONTEND_URL}/reset-password?token={raw_token}"
    html = get_password_reset_html(reset_url)
    send_email(user.email, "Reset your password", html)


def issue_reset_grant(db: Session, user) -> str:
    """After a reset code: a short-lived, single-use token that `/reset-password` takes like
    the emailed link's (the same table, so using either clears both)."""
    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=RESET_GRANT_MINUTES)
    create_password_reset_token(db, user.id, _hash_token(raw_token), expires_at)
    return raw_token


def reset_password(db: Session, token: str, new_password_hash: str) -> bool:
    """Validate token (hash lookup), update password, delete reset tokens, revoke refresh + sessions."""
    token_hash = _hash_token(token)
    pr = find_password_reset_by_token_hash(db, token_hash)
    if not pr:
        return False
    user_id = pr.user_id
    update_user_password(db, user_id, new_password_hash)
    delete_password_reset_tokens_for_user(db, user_id)
    revoke_all_for_user(db, user_id)
    delete_all_sessions_for_user(str(user_id))
    return True
