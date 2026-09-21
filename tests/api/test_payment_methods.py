"""Saved cards — the thing bidding cannot happen without.

A bid authorises money at the moment it is placed and re-authorises it weeks later with
nobody present, so the card has to be vaulted at Stripe first and referenced by id. These
endpoints are that step.
"""
from src.shared.models.user import User
from tests.factories import make_user


def test_starting_a_card_save_returns_everything_the_mobile_sheet_needs(
    db, client, auth_headers, stripe_enabled
):
    """Stripe's PaymentSheet needs all three of these together. Missing the ephemeral key
    means the sheet cannot show or reuse the customer's saved cards at all."""
    user = make_user(db)

    response = client.post("/api/payments/setup-intent", headers=auth_headers(user))

    assert response.status_code == 201, response.get_json()
    body = response.get_json()["data"]
    assert body["clientSecret"].startswith("seti_secret_")
    assert body["customerId"].startswith("cus_")
    assert body["ephemeralKeySecret"].startswith("ek_test_")


def test_the_customer_id_is_persisted_so_a_second_attempt_reuses_it(
    db, client, auth_headers, stripe_enabled
):
    """A new Stripe customer per attempt would scatter one person's saved cards across
    several customers, and the card they saved last week would be invisible this week."""
    user = make_user(db)

    first = client.post("/api/payments/setup-intent", headers=auth_headers(user))
    db.expire_all()
    stored = db.get(User, user.id).stripe_customer_id
    second = client.post("/api/payments/setup-intent", headers=auth_headers(user))

    assert stored, "the Stripe customer was created and then forgotten"
    assert first.get_json()["data"]["customerId"] == stored
    assert second.get_json()["data"]["customerId"] == stored
    assert len(stripe_enabled.customers) == 1


def test_saved_cards_come_back_as_ids_and_last_fours(db, client, auth_headers, stripe_enabled):
    user = make_user(db)
    client.post("/api/payments/setup-intent", headers=auth_headers(user))
    db.expire_all()
    customer_id = db.get(User, user.id).stripe_customer_id
    saved = stripe_enabled.add_card(customer_id, last4="4242")

    response = client.get("/api/payments/payment-methods", headers=auth_headers(user))

    assert response.status_code == 200
    methods = response.get_json()["data"]["paymentMethods"]
    assert len(methods) == 1
    assert methods[0]["id"] == saved["id"]
    assert methods[0]["last4"] == "4242"
    assert methods[0]["brand"] == "visa"
    # Nothing beyond what a picker needs to render.
    assert set(methods[0]) == {"id", "brand", "last4", "expMonth", "expYear"}


def test_a_user_who_has_never_bid_gets_an_empty_wallet_not_an_error(
    db, client, auth_headers, stripe_enabled
):
    user = make_user(db)

    response = client.get("/api/payments/payment-methods", headers=auth_headers(user))

    assert response.status_code == 200
    assert response.get_json()["data"]["paymentMethods"] == []


def test_you_cannot_detach_someone_elses_card(db, client, auth_headers, stripe_enabled):
    """The id comes from the client. Without an ownership check, guessing one would let
    anybody remove anybody else's saved card."""
    owner, attacker = make_user(db), make_user(db)
    client.post("/api/payments/setup-intent", headers=auth_headers(owner))
    client.post("/api/payments/setup-intent", headers=auth_headers(attacker))
    db.expire_all()
    victim_card = stripe_enabled.add_card(db.get(User, owner.id).stripe_customer_id)

    response = client.delete(
        f"/api/payments/payment-methods/{victim_card['id']}", headers=auth_headers(attacker)
    )

    assert response.status_code == 404
    assert stripe_enabled.detached == [], "detached a card belonging to someone else"


def test_the_owner_can_detach_their_own_card(db, client, auth_headers, stripe_enabled):
    user = make_user(db)
    client.post("/api/payments/setup-intent", headers=auth_headers(user))
    db.expire_all()
    card = stripe_enabled.add_card(db.get(User, user.id).stripe_customer_id)

    response = client.delete(
        f"/api/payments/payment-methods/{card['id']}", headers=auth_headers(user)
    )

    assert response.status_code == 200
    assert stripe_enabled.detached == [card["id"]]
    listed = client.get("/api/payments/payment-methods", headers=auth_headers(user))
    assert listed.get_json()["data"]["paymentMethods"] == []


def test_the_endpoints_require_a_signed_in_user(client):
    assert client.post("/api/payments/setup-intent").status_code == 401
    assert client.get("/api/payments/payment-methods").status_code == 401
