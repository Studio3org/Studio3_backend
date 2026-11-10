"""Phase 3: event ticketing (BACKEND_CHANGES B22, ticketing spec of 25 Oct 2026).

Covers the host's ticket types, the quote, buying (free, dev mode and through Stripe), the
10-minute hold and its sweep, a payment that lands after its hold, the free RSVP issuing a
real ticket, link-only events, and the ledger. Concurrency is in
tests/integration/test_ticket_stock.py.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.modules.events import ticketing_service
from src.shared.models.event import RSVP_GOING, EventRsvp
from src.shared.models.ticketing import (
    ORDER_EXPIRED,
    ORDER_PAID,
    ORDER_PENDING,
    TICKET_VOID,
    Ticket,
    TicketOrder,
)
from src.shared.ticketing import ticket_codes
from tests.factories import make_event, make_user
from tests.helpers import assert_ledger_balanced, seller_balance
from tests.stripe_fixtures import make_event as make_stripe_event
from tests.stripe_fixtures import post_webhook


def _published(db, host, **kw):
    return make_event(db, host, status="published", **kw)


def _add_type(client, auth, event, **body):
    payload = {"name": "General Admission", "priceCents": 2500, **body}
    response = client.post(f"/api/events/{event.id}/ticket-types", json=payload, headers=auth)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["id"]


def _detail(client, event, auth=None):
    return client.get(f"/api/events/{event.id}", headers=auth or {}).get_json()["data"]


def _order(client, event, auth, items):
    return client.post(f"/api/events/{event.id}/orders", json={"items": items}, headers=auth)


# --- the host -----------------------------------------------------------------------------

def test_a_host_sets_up_ticket_types_and_sees_their_share(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = _published(db, host)
    auth = auth_headers(host)

    vip = _add_type(client, auth, event, name="VIP", priceCents=2500, quantity=20,
                    perOrderLimit=4, includes="Drinks")
    _add_type(client, auth, event, name="Free entry", priceCents=0)

    as_host = {t["id"]: t for t in _detail(client, event, auth)["ticketTypes"]}
    assert as_host[vip]["hostNetCents"] == 2300, "spec: a $25 ticket = $23.00 to the host"
    assert as_host[vip]["left"] == 20 and as_host[vip]["perOrderLimit"] == 4

    public = _detail(client, event)
    assert public["isFree"] is False
    assert "hostNetCents" not in public["ticketTypes"][0], "the host's figures are the host's"


def test_paid_tickets_need_a_seller_profile(db, client, auth_headers):
    host = make_user(db)  # not a seller: no Stripe payouts
    event = _published(db, host)

    response = client.post(f"/api/events/{event.id}/ticket-types",
                           json={"name": "GA", "priceCents": 1000}, headers=auth_headers(host))

    assert response.status_code == 403
    assert "seller profile" in response.get_json()["message"]
    free = client.post(f"/api/events/{event.id}/ticket-types",
                       json={"name": "Free", "priceCents": 0}, headers=auth_headers(host))
    assert free.status_code == 201


def test_someone_else_cannot_manage_the_ticket_types(db, client, auth_headers):
    event = _published(db, make_user(db, seller=True))

    response = client.post(f"/api/events/{event.id}/ticket-types",
                           json={"name": "GA", "priceCents": 0}, headers=auth_headers(make_user(db)))

    assert response.status_code == 404


# --- buying --------------------------------------------------------------------------------

def test_the_quote_is_prices_plus_tax_and_no_fee(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = _published(db, host, refund_policy="7d")
    ga = _add_type(client, auth_headers(host), event, priceCents=2500)

    quote = client.post(f"/api/events/{event.id}/orders/quote",
                        json={"items": [{"ticketTypeId": ga, "quantity": 2}]},
                        headers=auth_headers(make_user(db))).get_json()["data"]

    assert quote["subtotalCents"] == 5000
    assert quote["serviceFeeCents"] == 0
    assert quote["taxCents"] == 413  # 8.25% of $50.00, half up
    assert quote["totalCents"] == 5413
    assert quote["refundPolicy"] == "Refunds up to 7 days before the event."


def test_buying_without_stripe_issues_signed_tickets_at_once(db, client, auth_headers):
    """Development without Stripe: like an art order's dev confirm, the tickets are issued
    straight away. Production always has Stripe."""
    host, buyer = make_user(db, seller=True), make_user(db)
    event = _published(db, host)
    ga = _add_type(client, auth_headers(host), event, priceCents=2500, quantity=10)
    auth = auth_headers(buyer)

    response = _order(client, event, auth, [{"ticketTypeId": ga, "quantity": 2}])

    assert response.status_code == 201, response.get_json()
    assert response.get_json()["data"]["clientSecret"] is None
    tickets = client.get(f"/api/user/me/tickets?eventId={event.id}", headers=auth).get_json()["data"]["tickets"]
    assert len(tickets) == 2
    assert all(t["number"].startswith("S3-T-") for t in tickets)
    assert ticket_codes.read_code(tickets[0]["code"]) == (
        __import__("uuid").UUID(tickets[0]["id"]), event.id)
    detail = _detail(client, event, auth)
    assert detail["ticketCount"] == 2 and detail["viewerIsGoing"] is True
    assert {t["id"]: t for t in detail["ticketTypes"]}[ga]["left"] == 8
    # The host is owed their share; the books balance.
    assert seller_balance(db, host.id) == 2 * 2300
    assert_ledger_balanced(db)


def test_free_tickets_are_issued_with_no_payment(db, client, auth_headers):
    host, guest = make_user(db, seller=True), make_user(db)
    event = _published(db, host)
    free = _add_type(client, auth_headers(host), event, name="Free", priceCents=0)

    response = _order(client, event, auth_headers(guest), [{"ticketTypeId": free, "quantity": 3}])

    assert response.status_code == 201
    assert response.get_json()["data"]["clientSecret"] is None
    assert response.get_json()["data"]["status"] == ORDER_PAID
    assert db.query(Ticket).filter_by(holder_id=guest.id).count() == 3


def test_limits_and_sold_out_are_refused(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = _published(db, host)
    ga = _add_type(client, auth_headers(host), event, priceCents=1000, quantity=3, perOrderLimit=2)
    later = _add_type(client, auth_headers(host), event, name="Late", priceCents=1000,
                      saleStartsAt=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())

    assert _order(client, event, auth_headers(make_user(db)),
                  [{"ticketTypeId": ga, "quantity": 3}]).status_code == 409, "over the per-order limit"
    assert _order(client, event, auth_headers(make_user(db)),
                  [{"ticketTypeId": later, "quantity": 1}]).status_code == 409, "sale not open yet"
    assert _order(client, event, auth_headers(make_user(db)),
                  [{"ticketTypeId": ga, "quantity": 2}]).status_code == 201
    assert _order(client, event, auth_headers(make_user(db)),
                  [{"ticketTypeId": ga, "quantity": 1}]).status_code == 201
    sold_out = _order(client, event, auth_headers(make_user(db)), [{"ticketTypeId": ga, "quantity": 1}])
    assert sold_out.status_code == 409
    assert "sold out" in sold_out.get_json()["message"]


# --- through Stripe ------------------------------------------------------------------------

def _paid_event(intent_id, order_id, event_id, event_name="payment_intent.succeeded", evt=None):
    return make_stripe_event(event_name, {
        "id": intent_id, "latest_charge": "ch_tickets_1", "amount": 2706, "amount_received": 2706,
        "metadata": {"kind": "ticket_order", "ticket_order_id": order_id, "event_id": event_id},
    }, event_id=evt)


def test_paying_through_stripe_issues_tickets_once(db, client, auth_headers, stripe_enabled):
    host, buyer = make_user(db, seller=True), make_user(db)
    event = _published(db, host)
    ga = _add_type(client, auth_headers(host), event, priceCents=2500, quantity=5)

    placed = _order(client, event, auth_headers(buyer), [{"ticketTypeId": ga, "quantity": 1}]).get_json()["data"]

    assert placed["clientSecret"] and placed["status"] == ORDER_PENDING
    left = {t["id"]: t for t in _detail(client, event)["ticketTypes"]}[ga]["left"]
    assert left == 4, "the checkout holds its ticket"

    order = db.get(TicketOrder, __import__("uuid").UUID(placed["id"]))
    stripe_enabled.add_charge("ch_tickets_1", fee_cents=109, amount_cents=order.total_cents)
    assert post_webhook(client, _paid_event(order.payment_reference, placed["id"], str(event.id),
                                            evt="evt_t1")).status_code == 200
    assert post_webhook(client, _paid_event(order.payment_reference, placed["id"], str(event.id),
                                            evt="evt_t2")).status_code == 200

    db.expire_all()
    assert db.get(TicketOrder, order.id).status == ORDER_PAID
    assert db.query(Ticket).filter_by(order_id=order.id).count() == 1
    assert_ledger_balanced(db)


def test_an_unpaid_hold_is_released_by_the_sweep(db, client, auth_headers, stripe_enabled):
    host, buyer = make_user(db, seller=True), make_user(db)
    event = _published(db, host)
    ga = _add_type(client, auth_headers(host), event, priceCents=2500, quantity=1)
    placed = _order(client, event, auth_headers(buyer), [{"ticketTypeId": ga, "quantity": 1}]).get_json()["data"]
    order = db.get(TicketOrder, __import__("uuid").UUID(placed["id"]))
    order.hold_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    assert ticketing_service.expire_holds() == 1

    db.expire_all()
    assert db.get(TicketOrder, order.id).status == ORDER_EXPIRED
    assert stripe_enabled.cancelled_intents == [order.payment_reference]
    assert {t["id"]: t for t in _detail(client, event)["ticketTypes"]}[ga]["left"] == 1


def test_money_after_the_hold_still_gets_tickets_if_they_are_there(
    db, client, auth_headers, stripe_enabled
):
    host, buyer = make_user(db, seller=True), make_user(db)
    event = _published(db, host)
    ga = _add_type(client, auth_headers(host), event, priceCents=2500, quantity=1)
    placed = _order(client, event, auth_headers(buyer), [{"ticketTypeId": ga, "quantity": 1}]).get_json()["data"]
    order = db.get(TicketOrder, __import__("uuid").UUID(placed["id"]))
    order.status = ORDER_EXPIRED
    db.commit()
    stripe_enabled.add_charge("ch_tickets_1", fee_cents=109, amount_cents=order.total_cents)

    post_webhook(client, _paid_event(order.payment_reference, placed["id"], str(event.id)))

    db.expire_all()
    assert db.get(TicketOrder, order.id).status == ORDER_PAID
    assert stripe_enabled.refunds == []


def test_money_after_the_hold_is_refunded_if_the_tickets_went_to_someone_else(
    db, client, auth_headers, stripe_enabled
):
    host, late, quick = make_user(db, seller=True), make_user(db), make_user(db)
    event = _published(db, host)
    ga = _add_type(client, auth_headers(host), event, priceCents=2500, quantity=1)
    placed = _order(client, event, auth_headers(late), [{"ticketTypeId": ga, "quantity": 1}]).get_json()["data"]
    order = db.get(TicketOrder, __import__("uuid").UUID(placed["id"]))
    order.status = ORDER_EXPIRED
    db.commit()
    # Someone else buys the last ticket in the meantime (dev-mode path issues at once).
    stripe_enabled.add_charge("ch_quick", fee_cents=0, amount_cents=2706)
    quick_order = _order(client, event, auth_headers(quick), [{"ticketTypeId": ga, "quantity": 1}]).get_json()["data"]
    quick_row = db.get(TicketOrder, __import__("uuid").UUID(quick_order["id"]))
    post_webhook(client, _paid_event(quick_row.payment_reference, quick_order["id"], str(event.id),
                                     evt="evt_quick"))
    stripe_enabled.add_charge("ch_tickets_1", fee_cents=109, amount_cents=order.total_cents)

    post_webhook(client, _paid_event(order.payment_reference, placed["id"], str(event.id), evt="evt_late"))

    assert [r["payment_intent"] for r in stripe_enabled.refunds] == [order.payment_reference]
    db.expire_all()
    assert db.query(Ticket).filter_by(order_id=order.id).count() == 0
    assert_ledger_balanced(db)


# --- the free RSVP --------------------------------------------------------------------------

def test_going_to_a_free_event_issues_a_ticket_and_not_going_returns_it(db, client, auth_headers):
    """Spec: free events and free-only orders skip payment — "Reserve creates the tickets
    right away, same QR codes"."""
    host, guest = make_user(db, seller=True), make_user(db)
    event = _published(db, host, capacity=50)
    auth = auth_headers(guest)

    going = client.post(f"/api/events/{event.id}/rsvp", json={"going": True}, headers=auth)

    assert going.status_code == 200 and going.get_json()["data"]["rsvpCount"] == 1
    tickets = client.get(f"/api/user/me/tickets?eventId={event.id}", headers=auth).get_json()["data"]["tickets"]
    assert [t["ticketTypeName"] for t in tickets] == ["General Admission"]

    assert client.post(f"/api/events/{event.id}/rsvp", json={"going": False}, headers=auth).status_code == 200
    db.expire_all()
    assert db.query(Ticket).filter_by(holder_id=guest.id).one().status == TICKET_VOID
    assert _detail(client, event, auth)["ticketCount"] == 0


def test_a_paid_only_event_sends_rsvp_to_checkout(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = _published(db, host)
    _add_type(client, auth_headers(host), event, priceCents=2500)

    response = client.post(f"/api/events/{event.id}/rsvp", json={"going": True},
                           headers=auth_headers(make_user(db)))

    assert response.status_code == 409
    assert "Choose tickets" in response.get_json()["message"]


def test_buying_a_ticket_counts_as_going(db, client, auth_headers):
    """Reminders, the guest list and Your events are built on RSVPs, so a ticket holder must
    be going without the app saying so separately."""
    host, buyer = make_user(db, seller=True), make_user(db)
    event = _published(db, host)
    ga = _add_type(client, auth_headers(host), event, priceCents=0)
    _order(client, event, auth_headers(buyer), [{"ticketTypeId": ga, "quantity": 1}])

    rsvp = db.execute(select(EventRsvp).where(EventRsvp.user_id == buyer.id)).scalar_one()
    assert rsvp.status == RSVP_GOING
    going = client.get("/api/events?scope=going", headers=auth_headers(buyer)).get_json()["data"]["events"]
    assert going[0]["ticketCount"] == 1


# --- event settings -------------------------------------------------------------------------

def test_a_link_only_event_is_not_listed_but_opens_from_its_link(db, client, auth_headers):
    host = make_user(db, seller=True)
    hidden = _published(db, host, title="Private view", visibility="link")
    _published(db, host, title="Open studio")

    titles = [e["title"] for e in client.get("/api/events?scope=upcoming").get_json()["data"]["events"]]

    assert "Open studio" in titles and "Private view" not in titles
    assert client.get(f"/api/events/{hidden.id}").status_code == 200


def test_the_refund_policy_is_fixed_once_paid_tickets_sell(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = _published(db, host, refund_policy="7d")
    auth = auth_headers(host)
    ga = _add_type(client, auth, event, priceCents=2500)
    assert client.patch(f"/api/events/{event.id}", json={"ageLimit": "21"}, headers=auth).status_code == 200

    _order(client, event, auth_headers(make_user(db)), [{"ticketTypeId": ga, "quantity": 1}])

    changed = client.patch(f"/api/events/{event.id}", json={"refundPolicy": "none"}, headers=auth)
    assert changed.status_code == 409
    detail = _detail(client, event)
    assert detail["refundPolicy"] == "7d" and detail["ageLimit"] == "21"


def test_a_forged_or_altered_code_is_rejected():
    import uuid

    code = ticket_codes.make_code(uuid.uuid4(), uuid.uuid4())
    tampered = code[:-2] + ("AA" if not code.endswith("AA") else "BB")

    assert ticket_codes.read_code(code) is not None
    assert ticket_codes.read_code(tampered) is None
    assert ticket_codes.read_code("S3T1.not-a-ticket") is None
    assert ticket_codes.read_code("") is None
