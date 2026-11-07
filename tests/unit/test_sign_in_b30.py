"""Sign-in by email (BACKEND_CHANGES B30), without a database or Redis: the email check, login
and reset codes, the lock after five wrong passwords, and the consent record.

Runs with `pytest tests/unit/test_sign_in_b30.py --noconftest`; the same over HTTP against
Postgres and Redis is in tests/api/test_sign_in_b30.py.
"""
import uuid
from types import SimpleNamespace

import pytest
from flask import Flask
from redis.exceptions import RedisError

from src.modules.auth import auth_controller
from src.modules.auth.services import login_lock, otp_service
from src.shared.constants import CURRENT_PRIVACY_VERSION, CURRENT_TERMS_VERSION
from src.shared.utils.app_error import AppError


class FakeRedis:
    """Enough of Redis for the code and lock keys (expiry is not simulated)."""

    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def setex(self, key, ttl, value):
        self.data[key] = value

    def incr(self, key):
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

    def expire(self, key, ttl):
        return key in self.data

    def exists(self, key):
        return int(key in self.data)

    def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)


@pytest.fixture()
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(otp_service, "get_redis_client", lambda: fake)
    monkeypatch.setattr(login_lock, "get_redis_client", lambda: fake)
    return fake


@pytest.fixture()
def app():
    return Flask(__name__)


# --- the lock -------------------------------------------------------------------------------------

def test_five_wrong_passwords_lock_and_a_good_log_in_clears(redis):
    user = uuid.uuid4()

    assert [login_lock.record_failure(user) for _ in range(5)] == [False, False, False, False, True]
    assert login_lock.is_locked(user)
    login_lock.clear(user)
    assert not login_lock.is_locked(user)
    assert login_lock.record_failure(user) is False, "the count started again"


def test_accounts_lock_on_their_own(redis):
    a, b = uuid.uuid4(), uuid.uuid4()
    for _ in range(5):
        login_lock.record_failure(a)

    assert login_lock.is_locked(a) and not login_lock.is_locked(b)


def test_without_redis_nobody_is_locked_out(monkeypatch):
    def down():
        raise RedisError("down")

    monkeypatch.setattr(login_lock, "get_redis_client", down)
    user = uuid.uuid4()

    assert login_lock.record_failure(user) is False
    assert login_lock.is_locked(user) is False
    login_lock.clear(user)


# --- codes ---------------------------------------------------------------------------------------

def test_a_limited_code_is_single_use(redis):
    otp_service.store_otp("login:a@b.co", "123456")

    assert otp_service.verify_otp_limited("login:a@b.co", "123456")
    assert not otp_service.verify_otp_limited("login:a@b.co", "123456")


def test_a_code_dies_after_five_wrong_guesses(redis):
    otp_service.store_otp("reset:a@b.co", "123456")

    assert not any(otp_service.verify_otp_limited("reset:a@b.co", "000000") for _ in range(4))
    assert otp_service.verify_otp_limited("reset:a@b.co", "123456"), "four wrong, then right"

    otp_service.store_otp("reset:a@b.co", "123456")
    for _ in range(5):
        otp_service.verify_otp_limited("reset:a@b.co", "000000")
    assert not otp_service.verify_otp_limited("reset:a@b.co", "123456"), "gone after five"


def test_each_purpose_has_its_own_code():
    keys = {p: auth_controller._otp_key(p, "a@b.co") for p in auth_controller.PURPOSES}

    assert keys == {"signup": "a@b.co", "login": "login:a@b.co", "reset": "reset:a@b.co"}


@pytest.mark.parametrize("body, purpose", [({}, "signup"), ({"purpose": " Login "}, "login"), ({"purpose": "reset"}, "reset")])
def test_the_purpose_defaults_to_sign_up(body, purpose):
    assert auth_controller._purpose(body) == purpose


def test_an_unknown_purpose_is_refused():
    with pytest.raises(AppError) as error:
        auth_controller._purpose({"purpose": "admin"})
    assert error.value.status_code == 400


# --- the controller ------------------------------------------------------------------------------

class _Db:
    def commit(self):
        pass

    def refresh(self, _):
        pass

    def close(self):
        pass


def _account(**kw):
    return SimpleNamespace(
        id=uuid.uuid4(), email="jordan@email.com", password="hash", deleted_at=None,
        terms_accepted_at=None, terms_version=None, privacy_version=None, **kw,
    )


@pytest.fixture()
def controller(monkeypatch, redis):
    """The controller with an account for jordan@email.com, a password of 'right', and sends,
    tokens and rate limits stubbed."""
    state = SimpleNamespace(user=_account(), sent=[], limited=[], grants=[])
    monkeypatch.setattr(auth_controller, "SessionLocal", _Db)
    monkeypatch.setattr(
        auth_controller, "find_user_by_email",
        lambda db, email: state.user if state.user and email == state.user.email else None,
    )
    monkeypatch.setattr(auth_controller, "_verify_password", lambda pw, h: pw == "right")
    monkeypatch.setattr(auth_controller, "send_email", lambda to, subject, html: state.sent.append((to, subject)) or True)
    monkeypatch.setattr(auth_controller, "generate_otp", lambda n: "654321")
    monkeypatch.setattr(auth_controller, "rate_limit_ip", lambda action, limit, window: state.limited.append(action))
    monkeypatch.setattr(
        auth_controller, "_issue_session_and_tokens",
        lambda user, req: ({"accessToken": f"token-{user.id}"}, 200, {"set_refresh_cookie": {}}),
    )

    def grant(db, user):
        state.grants.append(user.id)
        return "grant-1"

    monkeypatch.setattr(auth_controller, "issue_reset_grant", grant)
    return state


def _call(app, fn, body):
    with app.test_request_context(json=body):
        return fn()


def test_email_check_says_whether_an_account_exists_and_is_rate_limited(app, controller):
    known = _call(app, auth_controller.email_check, {"email": " Jordan@Email.com "})
    new = _call(app, auth_controller.email_check, {"email": "new@email.com"})

    assert known[0] == {"exists": True} and new[0] == {"exists": False}
    assert controller.limited == ["email_check", "email_check"]
    controller.user.deleted_at = "yesterday"
    assert _call(app, auth_controller.email_check, {"email": "jordan@email.com"})[0] == {"exists": False}


def test_email_check_needs_an_email(app, controller):
    with pytest.raises(AppError):
        _call(app, auth_controller.email_check, {"email": "nope"})


def test_a_login_code_goes_only_to_an_account(app, controller, redis):
    _call(app, auth_controller.otp_generate, {"email": "jordan@email.com", "purpose": "login"})
    _call(app, auth_controller.otp_generate, {"email": "new@email.com", "purpose": "login"})

    assert controller.sent == [("jordan@email.com", "Your login code")]
    assert redis.get("otp:login:jordan@email.com") == "654321"
    assert "otp:login:new@email.com" not in redis.data


def test_a_login_code_signs_in_and_lifts_the_lock(app, controller, redis):
    for _ in range(5):
        login_lock.record_failure(controller.user.id)
    _call(app, auth_controller.otp_generate, {"email": "jordan@email.com", "purpose": "login"})

    data, status, cookies = _call(
        app, auth_controller.otp_verify,
        {"email": "jordan@email.com", "otp": "654321", "purpose": "login", "acceptedTerms": True},
    )

    assert status == 200 and data == {"accessToken": f"token-{controller.user.id}"} and cookies
    assert not login_lock.is_locked(controller.user.id)
    assert controller.user.terms_version == CURRENT_TERMS_VERSION
    assert controller.user.privacy_version == CURRENT_PRIVACY_VERSION
    with pytest.raises(AppError):
        _call(app, auth_controller.otp_verify, {"email": "jordan@email.com", "otp": "654321", "purpose": "login"})


def test_a_sign_up_code_is_not_a_login(app, controller, redis):
    _call(app, auth_controller.otp_generate, {"email": "jordan@email.com"})

    with pytest.raises(AppError):
        _call(app, auth_controller.otp_verify, {"email": "jordan@email.com", "otp": "654321", "purpose": "login"})
    # The sign-up check is unchanged: it peeks and keeps the code.
    assert _call(app, auth_controller.otp_verify, {"email": "jordan@email.com", "otp": "654321"})[0] == {"verified": True}
    assert redis.get("otp:jordan@email.com") == "654321"


def test_a_reset_code_returns_a_grant_once(app, controller):
    _call(app, auth_controller.otp_generate, {"email": "jordan@email.com", "purpose": "reset"})

    data, status, _ = _call(app, auth_controller.otp_verify, {"email": "jordan@email.com", "otp": "654321", "purpose": "reset"})

    assert (data, status) == ({"resetToken": "grant-1"}, 200)
    assert controller.sent[-1] == ("jordan@email.com", "Your password reset code")
    with pytest.raises(AppError):
        _call(app, auth_controller.otp_verify, {"email": "jordan@email.com", "otp": "654321", "purpose": "reset"})
    assert controller.grants == [controller.user.id]


def test_the_sixth_wrong_password_is_refused_even_when_right(app, controller):
    body = {"username": "jordan@email.com", "password": "wrong"}
    statuses = []
    for _ in range(5):
        try:
            _call(app, auth_controller.login, body)
        except AppError as error:
            statuses.append(error.status_code)

    assert statuses == [401, 401, 401, 401, 429], "the fifth locks and says so"
    with pytest.raises(AppError) as error:
        _call(app, auth_controller.login, {"username": "jordan@email.com", "password": "right"})
    assert error.value.status_code == 429
    assert error.value.message == login_lock.LOCKED_MESSAGE


def test_a_right_password_starts_the_count_again(app, controller):
    for _ in range(4):
        with pytest.raises(AppError):
            _call(app, auth_controller.login, {"username": "jordan@email.com", "password": "wrong"})
    assert _call(app, auth_controller.login, {"username": "jordan@email.com", "password": "right"})[1] == 200
    with pytest.raises(AppError) as error:
        _call(app, auth_controller.login, {"username": "jordan@email.com", "password": "wrong"})
    assert error.value.status_code == 401


def test_reset_password_takes_the_grant(app, controller, monkeypatch):
    used = []
    monkeypatch.setattr(auth_controller, "do_reset_password", lambda db, token, h: used.append(token) or True)
    monkeypatch.setattr(auth_controller, "_hash_password", lambda pw: "h")

    _call(app, auth_controller.reset_password, {"resetToken": "grant-1", "newPassword": "new-password"})
    _call(app, auth_controller.reset_password, {"token": "link-token", "newPassword": "new-password"})

    assert used == ["grant-1", "link-token"]
