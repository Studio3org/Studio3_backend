"""Stripe Connect onboarding, on the Accounts v2 API.

This surface had no tests at all before the v2 migration — `connect_controller` was missing
from the `stripe_stub` patch list in conftest, so nothing could reach it. That is how the
`charges_enabled` gate below survived: it would have blocked every payout the first time an
artist actually finished onboarding, and no test would have noticed.
"""
from src.shared.config.stripe_client import payouts_ready
from src.shared.models.user import User
from tests.factories import make_user
from tests.stripe_fixtures import make_event, post_webhook


def _data(response) -> dict:
    """Routes wrap the controller payload in the {success, message, data} envelope."""
    return response.get_json()["data"]


def _seller_without_account(db):
    seller = make_user(db, seller=True)
    seller.stripe_account_id = None
    seller.stripe_payouts_enabled = False
    db.commit()
    return seller


# --- creating the account ------------------------------------------------------------------

def test_onboarding_creates_a_recipient_account_and_remembers_it(
    db, client, auth_headers, stripe_enabled
):
    seller = _seller_without_account(db)

    response = client.post("/api/artists/connect", headers=auth_headers(seller))

    assert response.status_code == 200
    body = _data(response)
    assert body["onboardingUrl"]
    db.expire_all()
    assert db.get(User, seller.id).stripe_account_id == body["stripeAccountId"]


def test_the_transfers_capability_is_actually_requested(db, client, auth_headers, stripe_enabled):
    """The artist exists to be paid. Without this capability Stripe refuses the transfer, and
    the failure surfaces at payout time — long after onboarding looked successful."""
    seller = _seller_without_account(db)

    client.post("/api/artists/connect", headers=auth_headers(seller))

    params = stripe_enabled.v2_account_requests[0]["params"]
    recipient = params["configuration"]["recipient"]
    assert recipient["capabilities"]["stripe_balance"]["stripe_transfers"]["requested"] is True
    assert "merchant" not in params["configuration"], (
        "the platform is the merchant of record; an artist never accepts a charge"
    )
    assert params["dashboard"] == "express"
    # Express obliges the platform to own fees and losses. Stripe rejects the call otherwise.
    assert params["defaults"]["responsibilities"] == {
        "fees_collector": "application",
        "losses_collector": "application",
    }


def test_the_onboarding_link_asks_for_the_recipient_flow(db, client, auth_headers, stripe_enabled):
    seller = _seller_without_account(db)

    client.post("/api/artists/connect", headers=auth_headers(seller))

    use_case = stripe_enabled.v2_account_link_requests[0]["use_case"]
    assert use_case["type"] == "account_onboarding"
    assert use_case["account_onboarding"]["configurations"] == ["recipient"]
    assert use_case["account_onboarding"]["return_url"]
    assert use_case["account_onboarding"]["refresh_url"]


def test_a_second_call_reuses_the_account_rather_than_creating_another(
    db, client, auth_headers, stripe_enabled
):
    """Account Links expire in minutes, so the app re-requests one routinely. Each re-request
    must not leave another connected account behind."""
    seller = _seller_without_account(db)

    first = client.post("/api/artists/connect", headers=auth_headers(seller))
    second = client.post("/api/artists/connect", headers=auth_headers(seller))

    assert len(stripe_enabled.v2_account_requests) == 1
    assert _data(first)["stripeAccountId"] == _data(second)["stripeAccountId"]


def test_the_link_expiry_is_a_unix_timestamp(db, client, auth_headers, stripe_enabled):
    """v1 sent an integer, v2 sends RFC 3339. The mobile app parses an integer, so the
    conversion belongs on this side of the wire."""
    seller = _seller_without_account(db)

    body = _data(client.post("/api/artists/connect", headers=auth_headers(seller)))

    assert isinstance(body["expiresAt"], int)
    assert body["expiresAt"] > 1_700_000_000


def test_onboarding_is_refused_when_stripe_is_not_configured(db, client, auth_headers):
    seller = _seller_without_account(db)

    response = client.post("/api/artists/connect", headers=auth_headers(seller))

    assert response.status_code == 503


# --- readiness, which gates every payout ----------------------------------------------------

def test_a_recipient_account_is_ready_without_charges_being_enabled():
    """The regression this file exists for.

    An artist is created with the recipient configuration only, so `charges_enabled` is false
    forever. The old gate was `payouts_enabled and charges_enabled`, which no real artist
    could ever satisfy.
    """
    onboarded = {"payouts_enabled": True, "charges_enabled": False,
                 "capabilities": {"transfers": "active"}}

    assert payouts_ready(onboarded) is True


def test_an_account_mid_onboarding_is_not_ready():
    assert payouts_ready(
        {"payouts_enabled": False, "capabilities": {"transfers": "inactive"}}
    ) is False
    # Bank details still missing: Stripe would accept the transfer and then sit on the money.
    assert payouts_ready(
        {"payouts_enabled": False, "capabilities": {"transfers": "active"}}
    ) is False
    # Capability withdrawn after the fact, e.g. a failed verification review.
    assert payouts_ready(
        {"payouts_enabled": True, "capabilities": {"transfers": "inactive"}}
    ) is False
    assert payouts_ready({"payouts_enabled": True}) is False


def test_status_reports_what_is_still_outstanding(db, client, auth_headers, stripe_enabled):
    seller = _seller_without_account(db)
    client.post("/api/artists/connect", headers=auth_headers(seller))

    body = _data(client.get("/api/artists/connect/status", headers=auth_headers(seller)))

    assert body["onboarded"] is False
    assert body["canListForSale"] is False
    assert "individual.first_name" in body["requirementsDue"]
    assert body["disabledReason"] == "requirements.past_due"


def test_every_status_branch_returns_the_same_shape(db, client, auth_headers, stripe_enabled):
    """Three different response shapes from one endpoint is how a client ends up with a null
    it never expected."""
    no_account = _seller_without_account(db)
    before = client.get("/api/artists/connect/status", headers=auth_headers(no_account))

    client.post("/api/artists/connect", headers=auth_headers(no_account))
    after = client.get("/api/artists/connect/status", headers=auth_headers(no_account))

    assert set(_data(before)) == set(_data(after))


# --- the webhook that is the real source of truth -------------------------------------------

def test_account_updated_marks_the_artist_payable(db, client, stripe_stub):
    seller = make_user(db, seller=True)
    seller.stripe_payouts_enabled = False
    db.commit()

    response = post_webhook(client, make_event("account.updated", {
        "id": seller.stripe_account_id,
        "payouts_enabled": True,
        "charges_enabled": False,
        "capabilities": {"transfers": "active"},
    }))

    assert response.status_code == 200
    db.expire_all()
    assert db.get(User, seller.id).stripe_payouts_enabled is True


def test_account_updated_withdraws_payability_again(db, client, stripe_stub):
    """Stripe can restrict an account after the fact. Missing this would keep paying out to an
    account that can no longer receive the money."""
    seller = make_user(db, seller=True)
    seller.stripe_payouts_enabled = True
    db.commit()

    post_webhook(client, make_event("account.updated", {
        "id": seller.stripe_account_id,
        "payouts_enabled": True,
        "charges_enabled": False,
        "capabilities": {"transfers": "inactive"},
    }))

    db.expire_all()
    assert db.get(User, seller.id).stripe_payouts_enabled is False


def test_account_updated_for_an_unknown_account_is_ignored(db, client, stripe_stub):
    response = post_webhook(client, make_event("account.updated", {
        "id": "acct_belongs_to_nobody",
        "payouts_enabled": True,
        "capabilities": {"transfers": "active"},
    }))

    assert response.status_code == 200
