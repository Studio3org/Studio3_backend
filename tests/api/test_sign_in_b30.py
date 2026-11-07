"""Sign-in by email over HTTP (BACKEND_CHANGES B30), against Postgres and Redis: the email check
is rate limited; a login code signs in; a reset code resets once; the sixth wrong password
inside 15 minutes is refused; agreeing records both versions.
"""
import bcrypt
import pytest

from src.shared.constants import CURRENT_PRIVACY_VERSION, CURRENT_TERMS_VERSION
from tests.factories import make_user


@pytest.fixture()
def sent(monkeypatch):
    """Emails the auth controller would send: (to, subject)."""
    out = []
    monkeypatch.setattr(
        "src.modules.auth.auth_controller.send_email",
        lambda to, subject, html: out.append((to, subject)) or True,
    )
    return out


def _account(db, password="right-password"):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    return make_user(db, password=hashed)


def _code(flush_redis, purpose, email):
    return flush_redis.get(f"otp:{purpose}:{email}")


def test_the_email_check_answers_and_is_rate_limited(db, client):
    user = _account(db)

    known = client.post("/api/auth/email/check", json={"email": user.email.upper()})
    new = client.post("/api/auth/email/check", json={"email": "nobody@example.test"})
    assert known.get_json()["data"] == {"exists": True}
    assert new.get_json()["data"] == {"exists": False}

    statuses = [client.post("/api/auth/email/check", json={"email": "x@example.test"}).status_code for _ in range(12)]
    assert 429 in statuses


def test_a_login_code_signs_in(db, client, flush_redis, sent):
    user = _account(db)

    client.post("/api/auth/otp/generate", json={"email": user.email, "purpose": "login"})
    code = _code(flush_redis, "login", user.email)
    response = client.post(
        "/api/auth/otp/verify",
        json={"email": user.email, "otp": code, "purpose": "login", "acceptedTerms": True},
    )

    assert sent == [(user.email, "Your login code")]
    assert response.status_code == 200, response.get_json()
    body = response.get_json()["data"]
    assert body["accessToken"] and body["user"]["username"] == user.username
    assert body["user"]["privacyVersion"] == CURRENT_PRIVACY_VERSION
    assert "refreshToken" in response.headers.get("Set-Cookie", "")
    again = client.post("/api/auth/otp/verify", json={"email": user.email, "otp": code, "purpose": "login"})
    assert again.status_code == 400


def test_no_login_code_is_sent_to_an_address_without_an_account(client, flush_redis, sent):
    response = client.post("/api/auth/otp/generate", json={"email": "nobody@example.test", "purpose": "login"})

    assert response.status_code == 200
    assert sent == []
    assert _code(flush_redis, "login", "nobody@example.test") is None


def test_a_reset_code_resets_once(db, client, flush_redis, sent):
    user = _account(db)

    client.post("/api/auth/otp/generate", json={"email": user.email, "purpose": "reset"})
    code = _code(flush_redis, "reset", user.email)
    grant = client.post(
        "/api/auth/otp/verify", json={"email": user.email, "otp": code, "purpose": "reset"}
    ).get_json()["data"]["resetToken"]

    first = client.post("/api/auth/reset-password", json={"resetToken": grant, "newPassword": "new-password-1"})
    second = client.post("/api/auth/reset-password", json={"resetToken": grant, "newPassword": "new-password-2"})

    assert first.status_code == 200, first.get_json()
    assert second.status_code == 400
    old = client.post("/api/auth/login", json={"username": user.email, "password": "right-password"})
    new = client.post("/api/auth/login", json={"username": user.email, "password": "new-password-1"})
    assert old.status_code == 401 and new.status_code == 200


def test_the_sixth_wrong_password_inside_15_minutes_is_refused(db, client, flush_redis, sent):
    user = _account(db)

    statuses = [
        client.post("/api/auth/login", json={"username": user.email, "password": "wrong"}).status_code
        for _ in range(5)
    ]
    sixth = client.post("/api/auth/login", json={"username": user.email, "password": "right-password"})

    assert statuses == [401, 401, 401, 401, 429]
    assert sixth.status_code == 429
    assert "code" in sixth.get_json()["message"]

    # The emailed code still works, and lifts the lock.
    client.post("/api/auth/otp/generate", json={"email": user.email, "purpose": "login"})
    code = _code(flush_redis, "login", user.email)
    assert client.post(
        "/api/auth/otp/verify", json={"email": user.email, "otp": code, "purpose": "login"}
    ).status_code == 200
    assert client.post("/api/auth/login", json={"username": user.email, "password": "right-password"}).status_code == 200


def test_agreeing_at_sign_up_records_the_moment_and_both_versions(db, client, flush_redis, sent):
    email = "new-person@example.test"
    client.post("/api/auth/otp/generate", json={"email": email})
    code = flush_redis.get(f"otp:{email}")

    response = client.post("/api/auth/register", json={
        "username": "newperson", "name": "New Person", "email": email,
        "password": "a-password", "otp": code, "acceptedTerms": True,
    })

    assert response.status_code == 200, response.get_json()
    user = response.get_json()["data"]["user"]
    assert user["termsAcceptedAt"]
    assert (user["termsVersion"], user["privacyVersion"]) == (CURRENT_TERMS_VERSION, CURRENT_PRIVACY_VERSION)
