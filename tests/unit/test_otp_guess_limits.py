"""SEC-3: every email code is guess-limited, not only the login and reset ones.

Sign-up's check-only step and the codes register() and email change spend used to accept
unlimited guesses, and a fresh code (three a minute) reset the per-code count.
"""
import pytest

from src.modules.auth.services import otp_service
from tests.unit.test_sign_in_b30_unit import FakeRedis


@pytest.fixture()
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(otp_service, "get_redis_client", lambda: fake)
    return fake


def test_codes_are_six_digits_from_a_secure_source(monkeypatch):
    calls = []
    monkeypatch.setattr(otp_service.secrets, "choice", lambda seq: calls.append(seq) or "7")

    assert otp_service.generate_otp(6) == "777777"
    assert len(calls) == 6


@pytest.mark.parametrize("check", [otp_service.verify_otp, otp_service.peek_otp])
def test_signup_and_email_change_codes_die_after_five_wrong_guesses(redis, check):
    otp_service.store_otp("a@b.co", "123456")

    for _ in range(5):
        assert not check("a@b.co", "000000")

    assert not check("a@b.co", "123456"), "the code is gone after five wrong guesses"


def test_peek_does_not_spend_the_code_but_register_does(redis):
    otp_service.store_otp("a@b.co", "123456")

    assert otp_service.peek_otp("a@b.co", "123456")
    assert otp_service.verify_otp("a@b.co", "123456")
    assert not otp_service.verify_otp("a@b.co", "123456")


def test_fresh_codes_do_not_buy_unlimited_guesses(redis):
    for _ in range(otp_service.FAILS_PER_HOUR // 4):
        otp_service.store_otp("login:a@b.co", "123456")
        for _ in range(4):
            otp_service.verify_otp_limited("login:a@b.co", "000000")
    otp_service.store_otp("login:a@b.co", "123456")
    for _ in range(otp_service.FAILS_PER_HOUR % 4):
        otp_service.verify_otp_limited("login:a@b.co", "000000")

    assert not otp_service.verify_otp_limited("login:a@b.co", "123456"), (
        "after an hour's worth of wrong guesses even the right code is refused"
    )


def test_a_mistype_or_two_still_lets_a_person_in(redis):
    otp_service.store_otp("login:a@b.co", "123456")

    assert not otp_service.verify_otp_limited("login:a@b.co", "123465")
    assert otp_service.verify_otp_limited("login:a@b.co", "123456")
