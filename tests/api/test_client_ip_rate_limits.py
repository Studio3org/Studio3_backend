"""AUTH-1: rate limits key on the client address the proxy saw, not one the client wrote.

nginx appends the connecting address to X-Forwarded-For, so the right-most entry is the one
we can trust and the left-most is whatever the client sent. Reading the left-most let anyone
start a fresh rate-limit bucket on every request.
"""
from src.modules.auth import auth_controller
from src.shared.utils import rate_limit


def _login(client, client_supplied: str, proxy_saw: str = "203.0.113.7"):
    return client.post(
        "/api/auth/login",
        json={"username": "nobody@example.com", "password": "wrong-password"},
        headers={"X-Forwarded-For": f"{client_supplied}, {proxy_saw}"},
    )


def test_a_forged_forwarded_for_does_not_reset_the_login_limit(client, flush_redis):
    for i in range(auth_controller.RATE_LOGIN_PER_IP):
        assert _login(client, f"10.0.0.{i}").status_code == 401

    assert _login(client, "10.0.0.250").status_code == 429


def test_a_different_real_client_has_its_own_limit(client, flush_redis):
    for i in range(auth_controller.RATE_LOGIN_PER_IP):
        _login(client, f"10.0.0.{i}")

    assert _login(client, "10.0.0.1", proxy_saw="198.51.100.9").status_code == 401


def test_login_refuses_rather_than_runs_unlimited_when_redis_is_down(client, monkeypatch):
    def down():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(rate_limit, "get_redis_client", down)

    assert _login(client, "10.0.0.1").status_code == 503
