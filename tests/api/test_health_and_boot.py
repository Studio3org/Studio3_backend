"""Health endpoints, boot-time config enforcement, and correlation ids."""
import pytest

from src.shared.config.sentry import _scrub
from src.shared.utils.request_id import current_request_id, request_id


# --- liveness vs readiness --------------------------------------------------------------

def test_root_is_liveness_and_touches_nothing_external(client, monkeypatch):
    """Render restarts the service when this fails, so a database blip must not reach it."""
    def _explode():
        raise AssertionError("liveness probe must not touch the database")

    monkeypatch.setattr("src.shared.config.database.check_db_connection", _explode)

    response = client.get("/")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_health_reports_every_dependency(client):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    checks = body["checks"]
    assert checks["database"]["ok"] is True
    assert checks["redis"]["ok"] is True
    # Booleans only — a health endpoint is unauthenticated.
    assert set(checks["stripe"]) == {"configured", "webhookSecretSet"}
    assert set(checks["s3"]) == {"configured", "bucketSet"}


def test_health_returns_503_when_the_database_is_down(client, monkeypatch):
    monkeypatch.setattr(
        "src.shared.config.database.check_db_connection",
        lambda: (False, "could not connect to server at db.internal:5432"),
    )

    response = client.get("/health")

    assert response.status_code == 503
    assert response.get_json()["status"] == "degraded"


def test_health_does_not_leak_the_connection_string_outside_development(client, monkeypatch):
    """The error text from psycopg2 contains the host, and this endpoint has no auth."""
    monkeypatch.setattr(
        "src.shared.config.database.check_db_connection",
        lambda: (False, "password authentication failed for user 'avnadmin'"),
    )

    body = client.get("/health").get_json()

    assert body["checks"]["database"]["error"] is None


# --- boot-time configuration ------------------------------------------------------------

def test_production_boot_fails_without_required_config(monkeypatch):
    from src.app import create_app
    from src.shared.config.settings import ENV_VARS

    for var in ENV_VARS:
        monkeypatch.delenv(var.name, raising=False)
        for alias in var.aliases:
            monkeypatch.delenv(alias, raising=False)
    monkeypatch.setenv("FLASK_ENV", "production")

    with pytest.raises(RuntimeError) as exc:
        create_app()

    message = str(exc.value)
    assert "STRIPE_WEBHOOK_SECRET" in message
    assert "DATABASE_URL" in message


# --- correlation ids --------------------------------------------------------------------

def test_response_carries_a_request_id(client):
    response = client.get("/")
    assert response.headers.get("X-Request-ID")


def test_a_supplied_request_id_is_echoed_back(client):
    response = client.get("/", headers={"X-Request-ID": "client-abc-123"})
    assert response.headers["X-Request-ID"] == "client-abc-123"


def test_a_hostile_request_id_is_sanitised():
    """The id goes straight into log lines and a response header, so a caller must not be
    able to forge entries or split the header.

    Tested against clean() rather than over HTTP: the test client refuses to send a newline
    in a header at all, which hides whether our own sanitising works.
    """
    from src.shared.utils.request_id import clean

    cleaned = clean("bad\nid with spaces\r\nX-Injected: yes" + "x" * 200)

    # Newline, carriage return and space are what would split a header or forge a log line.
    # ':' stays in the allowed set on purpose — our own ids are prefixed "evt:" / "auction-sweep:".
    assert not any(c in cleaned for c in "\n\r \t")
    assert "X-Injected" in cleaned, "sanitising should strip, not silently drop everything"
    assert len(cleaned) <= 64


def test_an_odd_but_legal_request_id_is_trimmed(client):
    response = client.get("/", headers={"X-Request-ID": "a" * 200})
    assert len(response.headers["X-Request-ID"]) <= 64


def test_request_id_context_nests_and_restores():
    assert current_request_id() == "-"
    with request_id("evt:evt_1"):
        assert current_request_id() == "evt:evt_1"
        with request_id("auction-sweep:abc"):
            assert current_request_id() == "auction-sweep:abc"
        assert current_request_id() == "evt:evt_1"
    assert current_request_id() == "-"


# --- Sentry scrubbing -------------------------------------------------------------------

def test_sentry_scrubs_buyer_pii():
    """StripeWebhookEvent.payload and the order address snapshot both carry buyer PII."""
    scrubbed = _scrub({
        "order": {
            "shipping_address_snapshot": {"line1": "1 Real St"},
            "total_cents": 12345,
        },
        "payload": {"data": {"object": {"receipt_email": "buyer@example.com"}}},
        "safe": "kept",
    })

    assert scrubbed["payload"] == "[scrubbed]"
    assert scrubbed["order"]["shipping_address_snapshot"] == "[scrubbed]"
    assert scrubbed["order"]["total_cents"] == 12345
    assert scrubbed["safe"] == "kept"
