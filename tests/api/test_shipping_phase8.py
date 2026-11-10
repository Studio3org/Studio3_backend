"""Phase 8: Convelio shipping (the Convelio plan's D-1 to D-12, B19, B31, B32), against a fake
Convelio that answers like the v2 API."""
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from src.modules.shipping import booking_service, shipping_jobs
from src.shared.models.notification import Notification
from src.shared.models.order import Order
from src.shared.models.shipment import Shipment
from src.shared.models.shipping import ProviderWebhookEvent, ShippingQuote
from tests.convelio_fixtures import KEY, signed_webhook  # noqa: F401
from tests.factories import make_address, make_piece, make_user
from tests.helpers import assert_ledger_balanced

def _shippable(db, *, price=3000_00):
    artist = make_user(db, seller=True)
    studio = make_address(db, artist, city="Brooklyn", state="NY", zip="11201", phone="(718) 555-0100")
    artist.pickup_address_id = studio.id
    piece = make_piece(db, artist, price_cents=price, status="live", package_length_cm=102,
                       package_width_cm=77, package_height_cm=11, weight_kg=5.4, title="Tide", medium="Oil on canvas")
    db.commit()
    return artist, piece


def _buyer(db):
    buyer = make_user(db)
    address = make_address(db, buyer, city="Los Angeles", state="CA", zip="90049")
    db.commit()
    return buyer, address


def _quote(client, auth, piece, address, **body):
    return client.post(f"/api/pieces/{piece.id}/shipping-quotes", headers=auth,
                       json={"addressId": str(address.id), **body})


def _notes(db, user, type_):
    return db.execute(select(Notification).where(Notification.user_id == user.id,
                                                 Notification.type == type_)).scalars().all()


# --- quotes ----------------------------------------------------------------------------------

def test_a_quote_sends_convelio_the_piece_and_both_addresses_as_it_expects(db, client, auth_headers, convelio):
    _, piece = _shippable(db)
    buyer, address = _buyer(db)

    response = _quote(client, auth_headers(buyer), piece, address, speed="express", deliveryType="white_glove")

    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert (data["shippingCents"], data["insuranceCents"], data["totalShippingCents"]) == (340_00, 25_00, 365_00)
    sent = convelio.quotes[0]
    item = sent["pickups"][0]["items"][0]
    assert (item["length"], item["width"], item["height"], item["weight"]) == (102, 77, 11, 6)
    assert item["value"] == {"amount": 3000_00, "currency_code": "USD"} and item["type"] == "fine_art.painting"
    assert sent["pickups"][0]["contacts"][0]["phone"] == "+17185550100"
    assert sent["delivery"]["type"] == "white_glove" and sent["shipping_speed"] == "express"
    assert sent["delivery"]["address"]["country_code"] == "US" and sent["contract_insurance"] is True


def test_a_piece_missing_its_packed_size_or_pickup_address_says_so(db, client, auth_headers, convelio):
    artist = make_user(db, seller=True)
    piece = make_piece(db, artist, price_cents=500_00, status="live")
    buyer, address = _buyer(db)

    response = _quote(client, auth_headers(buyer), piece, address)

    assert response.status_code == 409
    assert "packed length" in response.get_json()["message"]
    assert convelio.quotes == []


def test_checkout_pays_the_stored_quote_and_books_the_courier_once_paid(db, client, auth_headers, convelio):
    artist, piece = _shippable(db)
    buyer, address = _buyer(db)
    quote = _quote(client, auth_headers(buyer), piece, address).get_json()["data"]

    placed = client.post(f"/api/pieces/{piece.id}/collect", headers=auth_headers(buyer),
                         json={"addressId": str(address.id), "quoteId": quote["id"]})
    assert placed.status_code == 201, placed.get_json()
    order = placed.get_json()["data"]
    assert order["shippingCents"] == 365_00 and order["insuranceCents"] == 25_00
    assert order["totalCents"] == 3000_00 + 365_00 + order["taxCents"]
    assert convelio.orders == [], "nothing is booked before payment (D-5)"

    paid = client.post(f"/api/orders/{order['id']}/confirm", headers=auth_headers(buyer))
    assert paid.status_code == 200, paid.get_json()
    assert len(convelio.orders) == 1
    shipment = db.execute(select(Shipment)).scalar_one()
    assert shipment.booking_state == "booked" and shipment.provider_order_id == "o_1"
    assert shipment.actual_shipping_cost_cents == 365_00
    assert_ledger_balanced(db)


def test_without_a_quote_checkout_is_refused_while_convelio_is_on(db, client, auth_headers, convelio):
    _, piece = _shippable(db)
    buyer, address = _buyer(db)
    response = client.post(f"/api/pieces/{piece.id}/collect", headers=auth_headers(buyer),
                           json={"addressId": str(address.id), "shippingMethod": "standard"})
    assert response.status_code == 400 and "quote" in response.get_json()["message"]


def test_a_custom_quote_waits_for_convelio_then_the_buyer_is_told(db, client, auth_headers, convelio):
    _, piece = _shippable(db)
    buyer, address = _buyer(db)
    convelio.mode = "processing"
    quote = _quote(client, auth_headers(buyer), piece, address).get_json()["data"]
    assert quote["pending"] is True and quote["shippingCents"] is None

    early = client.post(f"/api/pieces/{piece.id}/collect", headers=auth_headers(buyer),
                        json={"addressId": str(address.id), "quoteId": quote["id"]})
    assert early.status_code == 409 and "still being priced" in early.get_json()["message"]

    provider_id = db.get(ShippingQuote, uuid.UUID(quote["id"])).provider_quote_id
    assert signed_webhook(client, {"event": "custom_quote_ready", "created": "now",
                            "payload": {"quote_id": provider_id}}).status_code == 200

    ready = client.get(f"/api/shipping-quotes/{quote['id']}", headers=auth_headers(buyer)).get_json()["data"]
    assert ready["status"] == "ready" and ready["totalShippingCents"] == 365_00
    assert len(_notes(db, buyer, "shipping_quote_ready")) == 1


# --- webhooks ------------------------------------------------------------------------------------

def test_webhooks_need_a_valid_signature_and_duplicates_are_ignored(db, client, convelio):
    raw = json.dumps({"event": "quote_paid", "payload": {}}).encode()
    bad = client.post("/api/webhooks/convelio", data=raw, content_type="application/json",
                      headers={"X-Convelio-signature": "0" * 64})
    assert bad.status_code == 401
    event = {"event": "quote_paid", "created": "x", "payload": {"quote_id": "q"}}
    assert signed_webhook(client, event).status_code == 200
    again = signed_webhook(client, event)
    assert again.status_code == 200 and again.get_json()["duplicate"] is True
    assert db.query(ProviderWebhookEvent).count() == 1


def _booked_order(db, client, auth_headers, convelio):
    artist, piece = _shippable(db)
    buyer, address = _buyer(db)
    quote = _quote(client, auth_headers(buyer), piece, address).get_json()["data"]
    order = client.post(f"/api/pieces/{piece.id}/collect", headers=auth_headers(buyer),
                        json={"addressId": str(address.id), "quoteId": quote["id"]}).get_json()["data"]
    client.post(f"/api/orders/{order['id']}/confirm", headers=auth_headers(buyer))
    return artist, piece, buyer, uuid.UUID(order["id"])


def test_convelio_statuses_move_the_order_and_tell_the_buyer(db, client, auth_headers, convelio):
    artist, piece, buyer, order_id = _booked_order(db, client, auth_headers, convelio)

    for status in ("shipment_created", "picked_up", "out_for_delivery", "shipment_completed"):
        assert signed_webhook(client, {"event": "shipment_status_changed", "created": status,
                                "payload": {"order_id": "o_1", "status": status}}).status_code == 200

    db.expire_all()
    order = db.get(Order, order_id)
    assert order.status == "awaiting_confirmation"
    shipment = db.execute(select(Shipment)).scalar_one()
    assert shipment.status == "delivered" and shipment.delivered_at is not None
    assert {n.type for n in db.query(Notification).filter_by(user_id=buyer.id)} >= {
        "shipment_picked_up", "shipment_out_for_delivery", "order_delivered"}
    seen = client.get(f"/api/pieces/{piece.id}", headers=auth_headers(buyer)).get_json()["data"]
    card = seen["viewerOrder"]
    assert card["side"] == "buyer" and card["shipment"]["trackingUrl"] == "https://track.convelio.test/x"
    assert all(step["done"] for step in card["shipment"]["steps"])
    assert client.get(f"/api/pieces/{piece.id}", headers=auth_headers(make_user(db))).get_json()["data"]["viewerOrder"] is None


def test_a_delivered_order_completes_itself_after_7_days_and_pays_the_artist(db, client, auth_headers, convelio):
    artist, piece, buyer, order_id = _booked_order(db, client, auth_headers, convelio)
    signed_webhook(client, {"event": "shipment_status_changed", "created": "x",
                     "payload": {"order_id": "o_1", "status": "shipment_completed"}})

    assert shipping_jobs.auto_complete_delivered() == 0, "not before the 7 days"
    db.execute(update(Shipment).values(delivered_at=datetime.now(timezone.utc) - timedelta(days=8)))
    db.commit()
    assert shipping_jobs.auto_complete_delivered() == 1
    db.expire_all()
    assert db.get(Order, order_id).status == "completed"
    assert_ledger_balanced(db)


def test_a_booking_that_times_out_is_never_retried_blind(db, client, auth_headers, convelio):
    convelio.order_mode = "timeout"
    artist, piece, buyer, order_id = _booked_order(db, client, auth_headers, convelio)
    shipment = db.execute(select(Shipment)).scalar_one()
    assert shipment.booking_state == "unknown"

    assert booking_service.book_order(str(order_id)) == {"state": "unknown"}, "no second booking"
    quote = db.execute(select(ShippingQuote)).scalar_one()
    signed_webhook(client, {"event": "order_created", "created": "x",
                     "payload": {"quote_id": quote.provider_quote_id, "order_id": "o_late"}})
    db.expire_all()
    shipment = db.execute(select(Shipment)).scalar_one()
    assert shipment.booking_state == "booked" and shipment.provider_order_id == "o_late"


def test_a_refused_booking_can_be_booked_again_by_ops(db, client, auth_headers, convelio):
    convelio.order_mode = "refuse"
    artist, piece, buyer, order_id = _booked_order(db, client, auth_headers, convelio)
    assert db.execute(select(Shipment.booking_state)).scalar() == "failed"

    convelio.order_mode = "ok"
    assert booking_service.book_order(str(order_id), retry=True)["state"] == "booked"


# --- estimate, pickup address, event pickup ----------------------------------------------------

def test_the_piece_page_estimate_falls_back_to_the_size_table(db, client, convelio):
    _, piece = _shippable(db)
    data = client.get(f"/api/pieces/{piece.id}/shipping-estimate?zip=90049").get_json()["data"]
    assert data["available"] is True and data["source"] == "table"
    assert data["lowCents"] < data["highCents"]

    convelio.estimate_mode = "ok"
    data = client.get(f"/api/pieces/{piece.id}/shipping-estimate").get_json()["data"]
    assert data["source"] == "convelio" and data["lowCents"] == 300_00

    bare = make_piece(db, make_user(db, seller=True), price_cents=100_00, status="live")
    assert client.get(f"/api/pieces/{bare.id}/shipping-estimate").get_json()["data"]["available"] is False


def test_the_pickup_address_is_the_artists_own_us_address_with_a_phone(db, client, auth_headers):
    artist = make_user(db, seller=True)
    abroad = make_address(db, artist, country="FR")
    no_phone = make_address(db, artist, phone="")
    good = make_address(db, artist, phone="212-555-0199")
    someone_elses = make_address(db, make_user(db))
    db.commit()
    auth = auth_headers(artist)

    assert client.put("/api/user/me/pickup-address", json={"addressId": str(abroad.id)}, headers=auth).status_code == 400
    assert client.put("/api/user/me/pickup-address", json={"addressId": str(no_phone.id)}, headers=auth).status_code == 400
    assert client.put("/api/user/me/pickup-address", json={"addressId": str(someone_elses.id)}, headers=auth).status_code == 404
    saved = client.put("/api/user/me/pickup-address", json={"addressId": str(good.id)}, headers=auth)
    assert saved.status_code == 200 and saved.get_json()["data"]["pickupAddress"]["id"] == str(good.id)
    piece = make_piece(db, artist, price_cents=100_00, status="live")
    ready = client.get(f"/api/pieces/{piece.id}", headers=auth).get_json()["data"]["shippingReady"]
    assert ready["ready"] is False and "weight" in ready["missing"]


def test_flat_rates_still_work_while_convelio_is_off(db, client, auth_headers):
    artist = make_user(db, seller=True)
    piece = make_piece(db, artist, price_cents=200_00, status="live")
    buyer, address = _buyer(db)
    quote = client.get(f"/api/pieces/{piece.id}/shipping-quote").get_json()["data"]
    assert quote["live"] is False and {m["id"] for m in quote["methods"]} >= {"standard", "express"}
    placed = client.post(f"/api/pieces/{piece.id}/collect", headers=auth_headers(buyer),
                         json={"addressId": str(address.id), "shippingMethod": "standard"})
    assert placed.status_code == 201 and placed.get_json()["data"]["shippingCents"] == 500


def test_stripe_tax_prices_the_artwork_and_the_shipping_by_address(db, client, auth_headers, convelio,
                                                                stripe_enabled, monkeypatch):
    class _Calc:
        calls = []

        @staticmethod
        def create(**kwargs):
            _Calc.calls.append(kwargs)
            return {"id": "taxcalc_1", "tax_amount_exclusive": 301_23}

    stripe_enabled.tax = type("T", (), {"Calculation": _Calc})
    monkeypatch.setattr("src.shared.config.stripe_client.get_stripe", lambda: stripe_enabled)
    monkeypatch.setattr("src.shared.config.stripe_client.stripe_configured", lambda: True)
    monkeypatch.setenv("SALES_TAX_PROVIDER", "stripe")
    _, piece = _shippable(db)
    buyer, address = _buyer(db)
    quote = _quote(client, auth_headers(buyer), piece, address).get_json()["data"]

    order = client.post(f"/api/pieces/{piece.id}/collect", headers=auth_headers(buyer),
                        json={"addressId": str(address.id), "quoteId": quote["id"]}).get_json()["data"]

    assert order["taxCents"] == 301_23
    call = _Calc.calls[0]
    assert call["shipping_cost"] == {"amount": 365_00}
    assert call["customer_details"]["address"]["state"] == "CA"
    assert db.get(Order, uuid.UUID(order["id"])).tax_calculation_id == "taxcalc_1"
