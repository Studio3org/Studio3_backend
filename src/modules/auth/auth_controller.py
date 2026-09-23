"""Auth: request/response, validation; calls services and DAOs; returns (data, status, cookie_ops)."""
import os
import uuid
import secrets
import bcrypt
from datetime import datetime, timezone, timedelta
from flask import request, g
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError

from src.shared.config.database import SessionLocal
from src.shared.utils.app_error import AppError
from src.shared.utils.messages import (
    OTP_VERIFICATION_FAILED,
    EMAIL_REQUIRED,
    LOGIN_IDENTIFIER_REQUIRED,
    USERNAME_REQUIRED,
    NAME_REQUIRED,
    PASSWORD_REQUIRED,
    INVALID_CREDENTIALS,
    USER_ALREADY_EXISTS,
    LOGOUT_SUCCESS,
    LOGOUT_ALL_SUCCESS,
    INVALID_RESET_TOKEN,
    UNAUTHORIZED,
)
from src.shared.utils.jwt_utils import sign_access_token
from src.shared.utils.rate_limit import rate_limit_ip
from src.shared.username.constants import RATE_CHECK_PER_IP, RATE_REGISTER_PER_IP
from src.shared.username.normalize import normalize
from src.shared.username.availability import check_availability, invalidate_username_cache
from src.shared.notification.email_service import send_email
from src.shared.templates.otp_template import get_otp_html
from src.modules.auth.auth_dao import (
    find_user_by_email,
    find_user_by_username_or_history,
    create_user,
)
from src.modules.auth.services.otp_service import (
    acquire_lock,
    check_resend_limit,
    store_otp,
    verify_otp,
    peek_otp,
    generate_otp,
)
from src.modules.auth.services.password_reset_service import (
    request_password_reset,
    reset_password as do_reset_password,
)
from src.modules.sessions.session_service import (
    create_session,
    delete_session,
    delete_all_sessions_for_user,
)
from src.modules.sessions.refresh_token_dao import (
    create as create_refresh_token,
    find_by_id,
    revoke_by_id,
    revoke_all_for_user,
)
from src.modules.user.user_serializers import user_to_dict
from src.shared.config.cors import refresh_cookie_flags

SALT_ROUNDS = int(os.getenv("SALT_ROUNDS", "10"))
# Sessions/refresh tokens never expire on their own — only explicit logout
# (or logout-all-devices) ends one. Cookies and the DB's `expires_at`
# column need *some* concrete value, so a far-future date stands in for
# "never" rather than a real expiry.
REFRESH_TOKEN_DAYS = 365 * 100
COOKIE_MAX_AGE = REFRESH_TOKEN_DAYS * 24 * 3600
MIN_PASSWORD_LEN = 8


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=SALT_ROUNDS)).decode()


def _verify_password(password: str, pwd_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), pwd_hash.encode())


def _hash_refresh_secret(secret: str) -> str:
    return bcrypt.hashpw(secret.encode(), bcrypt.gensalt(rounds=SALT_ROUNDS)).decode()


def _issue_session_and_tokens(user, request_obj=None):
    db = SessionLocal()
    try:
        user_id = str(user.id)
        user_agent = request_obj.headers.get("User-Agent", "") if request_obj else ""
        ip = request_obj.remote_addr if request_obj else ""
        session_id = create_session(user_id, user_agent, ip)
        access_token = sign_access_token({"sub": user_id, "sessionId": session_id})

        secret = secrets.token_hex(32)
        hashed = _hash_refresh_secret(secret)
        expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_DAYS)
        row = create_refresh_token(db, user.id, hashed, session_id, expires_at)
        cookie_value = f"{row.id}.{secret}"

        cookie_opts = {
            "value": cookie_value,
            "max_age": COOKIE_MAX_AGE,
            **refresh_cookie_flags(request_obj),
        }

        return (
            {"accessToken": access_token, "user": user_to_dict(db, user)},
            200,
            {"set_refresh_cookie": cookie_opts},
        )
    finally:
        db.close()


def username_check():
    rate_limit_ip("username_check", RATE_CHECK_PER_IP, 60)
    raw = (request.args.get("username") or "").strip()
    for_user_id = None
    if request.args.get("for_user_id") == "me" and hasattr(g, "user") and g.user:
        for_user_id = uuid.UUID(g.user["id"])

    db = SessionLocal()
    try:
        result = check_availability(db, raw, exclude_user_id=for_user_id, include_suggestions=True)
        return {
            "available": result.available,
            "normalized": result.normalized,
            "reason": result.reason,
            "message": result.message,
            "suggestions": result.suggestions,
        }, 200, None
    finally:
        db.close()


def otp_generate():
    body = request.get_json() or {}
    email = (body.get("email") or "").strip().lower()
    if not email:
        raise AppError(EMAIL_REQUIRED, 400)
    if not acquire_lock(email):
        raise AppError("Please wait before requesting another OTP.", 429)
    if not check_resend_limit(email):
        raise AppError("Resend limit exceeded. Try again later.", 429)
    otp = generate_otp(6)
    store_otp(email, otp)
    if not send_email(email, "Your verification code", get_otp_html(otp)):
        raise AppError("Could not send the verification email. Please try again.", 502)
    return {"message": "OTP sent successfully."}, 200, None


def otp_resend():
    return otp_generate()


def otp_verify():
    """Check-only step used by the sign-up wizard's OTP screen — does not
    consume the code or create an account. The real, single-use check still
    happens in register() via verify_otp()."""
    body = request.get_json() or {}
    email = (body.get("email") or "").strip().lower()
    otp = (body.get("otp") or "").strip()
    if not email:
        raise AppError(EMAIL_REQUIRED, 400)
    if not otp or not peek_otp(email, otp):
        raise AppError(OTP_VERIFICATION_FAILED, 400)
    return {"verified": True}, 200, None


def register():
    rate_limit_ip("register", RATE_REGISTER_PER_IP, 60)
    body = request.get_json() or {}
    username_raw = (body.get("username") or "").strip()
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password")
    otp = (body.get("otp") or "").strip()
    phone = (body.get("phone") or "").strip() or None

    if not username_raw:
        raise AppError(USERNAME_REQUIRED, 400)
    if not name:
        raise AppError(NAME_REQUIRED, 400)
    if not email:
        raise AppError(EMAIL_REQUIRED, 400)
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise AppError(PASSWORD_REQUIRED if not password else "Password must be at least 8 characters.", 400)
    if not otp:
        raise AppError(OTP_VERIFICATION_FAILED, 400)
    if not verify_otp(email, otp):
        raise AppError(OTP_VERIFICATION_FAILED, 400)

    norm = normalize(username_raw)
    if not norm.ok or not norm.normalized:
        raise AppError("Invalid username format.", 400)

    db = SessionLocal()
    try:
        if find_user_by_email(db, email):
            raise AppError(USER_ALREADY_EXISTS, 409)

        avail = check_availability(db, norm.normalized, include_suggestions=True)
        if not avail.available:
            msg = avail.message
            if avail.suggestions:
                msg += f" Try: {', '.join(avail.suggestions[:3])}"
            raise AppError(msg, 409)

        try:
            user = create_user(
                db,
                username=norm.normalized,
                email=email,
                name=name,
                password_hash=_hash_password(password),
                email_verified=True,
                phone=phone,
            )
        except IntegrityError:
            db.rollback()
            raise AppError("Username or email was just taken. Try another.", 409)

        invalidate_username_cache(norm.normalized)
        return _issue_session_and_tokens(user, request)
    finally:
        db.close()


def login():
    body = request.get_json() or {}
    identifier_raw = (body.get("username") or "").strip()
    password = body.get("password")
    if not identifier_raw:
        raise AppError(LOGIN_IDENTIFIER_REQUIRED, 400)
    if not password:
        raise AppError(PASSWORD_REQUIRED, 400)

    db = SessionLocal()
    try:
        if "@" in identifier_raw:
            user = find_user_by_email(db, identifier_raw.lower())
        else:
            norm = normalize(identifier_raw)
            if not norm.ok or not norm.normalized:
                raise AppError(INVALID_CREDENTIALS, 401)
            user, _ = find_user_by_username_or_history(db, norm.normalized)

        if not user or not user.password or user.deleted_at:
            raise AppError(INVALID_CREDENTIALS, 401)
        if not _verify_password(password, user.password):
            raise AppError(INVALID_CREDENTIALS, 401)
        return _issue_session_and_tokens(user, request)
    finally:
        db.close()


def refresh():
    cookie = request.cookies.get("refreshToken")
    if not cookie or "." not in cookie:
        raise AppError(UNAUTHORIZED, 401)
    parts = cookie.split(".", 1)
    token_id_str, secret = parts[0], parts[1]
    try:
        token_id = uuid.UUID(token_id_str)
    except ValueError:
        raise AppError(UNAUTHORIZED, 401)
    db = SessionLocal()
    try:
        row = find_by_id(db, token_id)
        if not row or row.is_revoked or row.expires_at < datetime.now(timezone.utc):
            raise AppError(UNAUTHORIZED, 401)
        if not bcrypt.checkpw(secret.encode(), row.hashed_token.encode()):
            raise AppError(UNAUTHORIZED, 401)
        from src.modules.sessions.session_service import find_session as _find_session
        try:
            session_found = _find_session(row.session_id) is not None
        except RedisError:
            # Redis being unreachable is an infrastructure hiccup, not a
            # real logout — don't reject a refresh just because we
            # couldn't ask Redis to confirm the session still exists.
            session_found = True
        if not session_found:
            raise AppError(UNAUTHORIZED, 401)
        from src.shared.models.user import User
        user = db.get(User, row.user_id)
        if not user:
            raise AppError(UNAUTHORIZED, 401)
        revoke_by_id(db, token_id)
        return _issue_session_and_tokens(user, request)
    finally:
        db.close()


def logout():
    cookie = request.cookies.get("refreshToken")
    if cookie and "." in cookie:
        parts = cookie.split(".", 1)
        try:
            token_id = uuid.UUID(parts[0])
        except ValueError:
            pass
        else:
            db = SessionLocal()
            try:
                row = find_by_id(db, token_id)
                if row:
                    revoke_by_id(db, token_id)
                    delete_session(row.session_id)
            finally:
                db.close()
    return {"message": LOGOUT_SUCCESS}, 200, {"clear_refresh_cookie": refresh_cookie_flags()}


def logout_all():
    user_id = g.user.get("id")
    if not user_id:
        raise AppError(UNAUTHORIZED, 401)
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise AppError(UNAUTHORIZED, 401)
    db = SessionLocal()
    try:
        revoke_all_for_user(db, uid)
    finally:
        db.close()
    delete_all_sessions_for_user(user_id)
    return {"message": LOGOUT_ALL_SUCCESS}, 200, {"clear_refresh_cookie": refresh_cookie_flags()}


def forget_password():
    body = request.get_json() or {}
    email = (body.get("email") or "").strip().lower()
    if not email:
        raise AppError(EMAIL_REQUIRED, 400)
    db = SessionLocal()
    try:
        request_password_reset(db, email)
    finally:
        db.close()
    return {"message": "If an account exists with this email, you will receive a password reset link."}, 200, None


def reset_password():
    body = request.get_json() or {}
    token = (body.get("token") or "").strip()
    new_password = body.get("newPassword")
    if not token:
        raise AppError(INVALID_RESET_TOKEN, 400)
    if not new_password or len(new_password) < MIN_PASSWORD_LEN:
        raise AppError("New password must be at least 8 characters.", 400)
    db = SessionLocal()
    try:
        ok = do_reset_password(db, token, _hash_password(new_password))
    finally:
        db.close()
    if not ok:
        raise AppError(INVALID_RESET_TOKEN, 400)
    return {"message": "Password has been reset successfully."}, 200, None


def change_password():
    """Authenticated password change (distinct from the email-token forget/reset flow above).
    Invalidates other active sessions, same as logout-all, since a password change should
    kill any sessions started before the credential was known to be compromised/changed —
    but immediately reissues a fresh session/token pair for THIS device/request, so the
    user isn't also logged out of the device where they just changed their password."""
    from src.modules.auth.auth_dao import update_user_password
    from src.shared.models.user import User

    body = request.get_json() or {}
    current_password = body.get("currentPassword")
    new_password = body.get("newPassword")
    if not current_password:
        raise AppError("Current password is required.", 400)
    if not new_password or len(new_password) < MIN_PASSWORD_LEN:
        raise AppError("New password must be at least 8 characters.", 400)

    user_id = uuid.UUID(g.user["id"])
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user or not user.password or not _verify_password(current_password, user.password):
            raise AppError("Current password is incorrect.", 401)
        update_user_password(db, user_id, _hash_password(new_password))
        revoke_all_for_user(db, user_id)
    finally:
        db.close()

    delete_all_sessions_for_user(str(user_id))

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        return _issue_session_and_tokens(user, request)
    finally:
        db.close()


# Fixed set, same pattern as REPORT_REASONS — a dropdown, not a free-for-all, so the answers
# an operator sees are actually comparable to each other. "other" is the escape hatch, backed
# by the free-text `feedback` field.
DELETION_REASONS = (
    "not_using",
    "found_alternative",
    "fees_too_high",
    "privacy_concerns",
    "too_many_notifications",
    "technical_issues",
    "poor_support",
    "other",
)


def delete_account():
    """Anonymize and deactivate the caller's account.

    Not a hard DELETE — see the deleted_at column comment on the User model. Blocks on the
    same in-flight-obligation checks as seller_disable (active listings, in-progress sales),
    since an account can't be allowed to vanish out from under an active auction or order.

    Asks why, and that answer is the one thing about this account worth keeping: it is
    recorded as an audit event with this user's *real* name/username still in the label,
    before anything below gets anonymized — see AUDIT_ACCOUNT_DELETED's own comment for why
    that ordering matters. An operator reading the admin audit log is the "it will appear on
    the admin account" this exists for.
    """
    from src.modules.orders import orders_dao
    from src.modules.pieces.pieces_dao import list_user_pieces
    from src.modules.bids.auction_cancellation import REASON_SELLER_DEACTIVATED
    from src.modules.pieces import listing_service
    from src.shared.models.user import User
    from src.shared.models import audit
    from src.shared.models.audit import ACTOR_USER
    from src.modules.admin import audit_service

    body = request.get_json() or {}
    password = body.get("password")
    if not password:
        raise AppError("Password is required to delete your account.", 400)
    reason = (body.get("reason") or "").strip().lower()
    if reason and reason not in DELETION_REASONS:
        raise AppError("Choose a reason for deleting your account.", 400)
    reason = reason or None
    feedback = (body.get("feedback") or "").strip() or None
    if feedback and len(feedback) > 1000:
        feedback = feedback[:1000]

    user_id = uuid.UUID(g.user["id"])
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user or not user.password or not _verify_password(password, user.password):
            raise AppError("Password is incorrect.", 401)

        active_listings = list_user_pieces(db, user.id, for_sale_only=True)
        if active_listings:
            raise AppError(
                "Remove your active listings before deleting your account.", 400
            )
        in_progress = orders_dao.count_seller_in_progress(db, user.id)
        if in_progress:
            raise AppError(
                "You have in-progress sales — complete or cancel them before deleting your account.",
                400,
            )
        buyer_in_progress = orders_dao.count_buyer_in_progress(db, user.id)
        if buyer_in_progress:
            raise AppError(
                "You have in-progress purchases — complete or cancel them before deleting your account.",
                400,
            )

        # Belt-and-suspenders, same as seller_disable: a live auction with bids isn't
        # caught by the for-sale check above, and can't be left to expire unattended
        # once nobody is behind the account to receive its payout.
        listing_service.delist_all_for_seller(db, user.id, reason=REASON_SELLER_DEACTIVATED)

        # Recorded here, before a single field below is overwritten: audit_service reads
        # user.name/user.username *now* to build actor_label, and there is no later point at
        # which those still say who this really was. commit=False folds it into the same
        # transaction as the anonymization that follows, so the two can never disagree about
        # whether this account was actually deleted.
        audit_service.record(
            db, audit.AUDIT_ACCOUNT_DELETED,
            actor=user, actor_type=ACTOR_USER,
            subject_type="user", subject_id=user.id,
            detail={"reason": reason, "feedback": feedback},
            note=(reason.replace('_', ' ') if reason else "not given") + (f": {feedback}" if feedback else ""),
            commit=False,
        )

        anon = f"deleted_{user.id.hex[:12]}"
        user.username = anon
        user.email = f"{anon}@deleted.studio-3.co"
        user.name = "Deleted user"
        user.password = None
        user.image = None
        user.cover_photo_url = None
        user.bio = None
        user.location = None
        user.phone = None
        user.latitude = None
        user.longitude = None
        user.seller_enabled = False
        user.website = None
        user.instagram = None
        user.twitter = None
        user.tags = None
        user.taste_preferences = None
        user.deleted_at = datetime.now(timezone.utc)
        db.commit()

        revoke_all_for_user(db, user_id)
    finally:
        db.close()

    delete_all_sessions_for_user(str(user_id))
    return {"deleted": True}, 200


def _email_change_otp_key(new_email: str) -> str:
    # Prefixed so it never collides with a registration OTP for the same address.
    return f"email_change:{new_email}"


def request_email_change():
    """Step 1 of authenticated email change: send an OTP to the NEW address before
    anything is written, mirroring register()'s verify-before-write pattern."""
    from src.modules.auth.auth_dao import find_user_by_email

    body = request.get_json() or {}
    new_email = (body.get("newEmail") or "").strip().lower()
    if not new_email:
        raise AppError(EMAIL_REQUIRED, 400)

    db = SessionLocal()
    try:
        if find_user_by_email(db, new_email):
            raise AppError("This email is already in use.", 409)
    finally:
        db.close()

    key = _email_change_otp_key(new_email)
    if not acquire_lock(key):
        raise AppError("Please wait before requesting another code.", 429)
    if not check_resend_limit(key):
        raise AppError("Resend limit exceeded. Try again later.", 429)
    otp = generate_otp(6)
    store_otp(key, otp)
    send_email(new_email, "Confirm your new email", get_otp_html(otp))
    return {"message": "Verification code sent to the new email address."}, 200, None


def confirm_email_change():
    """Step 2: verify the OTP sent to the new address, then swap User.email."""
    from src.modules.auth.auth_dao import find_user_by_email, update_user_email

    body = request.get_json() or {}
    new_email = (body.get("newEmail") or "").strip().lower()
    otp = (body.get("otp") or "").strip()
    if not new_email:
        raise AppError(EMAIL_REQUIRED, 400)
    if not otp or not verify_otp(_email_change_otp_key(new_email), otp):
        raise AppError(OTP_VERIFICATION_FAILED, 400)

    user_id = uuid.UUID(g.user["id"])
    db = SessionLocal()
    try:
        if find_user_by_email(db, new_email):
            raise AppError("This email is already in use.", 409)
        update_user_email(db, user_id, new_email)
    finally:
        db.close()
    return {"email": new_email}, 200, None
