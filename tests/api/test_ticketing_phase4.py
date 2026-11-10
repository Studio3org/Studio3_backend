"""Phase 4: ticket refunds, the waitlist, check-in, host cancel and reschedule, host payouts,
and savers told when a sold-out event reopens (BACKEND_CHANGES B22, B26, B28; ticketing spec
of 25 Oct 2026)."""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from src.modules.events import host_payouts, waitlist_service
from src.shared.models.event import EventSave
from src.shared.models.notification import Notification
from src.shared.models.ticketing import (
    DEBT_OPEN,
    DEBT_SETTLED,
    EVENT_PAYOUT_RELEASED,
    HOLD_ACTIVE,
    ORDER_PARTLY_REFUNDED,
    ORDER_REFUNDED,
    TICKET_REFUNDED,
    TICKET_VOID,
    WAITLIST_BOUGHT,
    WAITLIST_EXPIRED,
    EventPayout,
    HostFeeDebt,
    Ticket,
    TicketOrder,
    WaitlistEntry,
    WaitlistHold,
)
from tests.factories import make_event, make_event_participant, make_user
from tests.helpers import assert_ledger_balanced, run_concurrently, seller_balance
from tests.stripe_fixtures import make_event as make_stripe_event
from tests.stripe_fixtures import post_webhook


def _now():
    return datetime.now(timezone.utc)


def _event(db, host, **kw):
    kw.setdefault("refund_policy", "7d")
    kw.setdefault("starts_in", timedelta(days=14))
    return make_event(db, host, status="published", **kw)


def _add_type(client, auth, event, **body):
    payload = {"name": "General Admission", "priceCents": 2500, **body}
    response = client.post(f"/api/events/{event.id}/ticket-types", json=payload, headers=auth)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["id"]


def _buy(client, event, auth, type_id, quantity=1):
    response = client.post(f"/api/events/{event.id}/orders",
                           json={"items": [{"ticketTypeId": type_id, "quantity": quantity}]},
                           headers=auth)
    return response


def _tickets(client, event, auth):
    return client.get(f"/api/user/me/tickets?eventId={event.id}",
                      headers=auth).get_json()["data"]["tickets"]


def _detail(client, event, auth=None):
    return client.get(f"/api/events/{event.id}", headers=auth or {}).get_json()["data"]


def _notes(db, user, type_):
    return db.execute(select(Notification).where(Notification.user_id == user.id,
                                                 Notification.type == type_)).scalars().all()


# --- buyer refunds ---------------------------------------------------------------------------

def test_a_buyer_refunds_one_ticket_then_the_rest_and_every_cent_comes_back(
    db, client, auth_headers, stripe_enabled
):
    host, buyer = make_user(db, seller=True), make_user(db)
    event = _event(db, host)
    ga = _add_type(client, auth_headers(host), event, priceCents=2500, quantity=10)
    placed = _buy(client, event, auth_headers(buyer), ga, 3).get_json()["data"]
    order = db.get(TicketOrder, uuid.UUID(placed["id"]))
    stripe_enabled.add_charge("ch_p4", fee_cents=250, amount_cents=order.total_cents)
    post_webhook(client, make_stripe_event("payment_intent.succeeded", {
        "id": order.payment_reference, "latest_charge": "ch_p4", "amount": order.total_cents,
        "amount_received": order.total_cents,
        "metadata": {"kind": "ticket_order", "ticket_order_id": placed["id"]},
    }))
    auth = auth_headers(buyer)
    tickets = _tickets(client, event, auth)
    assert len(tickets) == 3 and all(t["refundable"] for t in tickets)

    one = client.post(f"/api/events/{event.id}/orders/{placed['id']}/refund",
                      json={"ticketIds": [tickets[0]["id"]]}, headers=auth)
    assert one.status_code == 200, one.get_json()
    assert one.get_json()["data"]["status"] == ORDER_PARTLY_REFUNDED
    assert one.get_json()["data"]["refund"]["amountCents"] == 2500 + 206  # 8.25% of $25

    rest = client.post(f"/api/events/{event.id}/orders/{placed['id']}/refund", json={}, headers=auth)
    assert rest.status_code == 200, rest.get_json()
    assert rest.get_json()["data"]["status"] == ORDER_REFUNDED

    db.expire_all()
    order = db.get(TicketOrder, order.id)
    assert sum(r["amount"] for r in stripe_enabled.refunds) == order.total_cents
    assert all(r["payment_intent"] == order.payment_reference for r in stripe_enabled.refunds)
    assert order.refunded_cents == order.total_cents
    assert seller_balance(db, host.id) == 0, "the host earns nothing on refunded tickets"
    assert _tickets(client, event, auth) == []
    assert _detail(client, event, auth)["viewerIsGoing"] is False
    assert_ledger_balanced(db)


def test_refunds_close_at_the_window_and_never_open_on_a_no_refund_event(db, client, auth_headers):
    host, buyer = make_user(db, seller=True), make_user(db)
    near = _event(db, host, starts_in=timedelta(days=3))  # 7-day policy: already closed
    never = _event(db, host, refund_policy="none")
    auth = auth_headers(buyer)
    for event in (near, never):
        ga = _add_type(client, auth_headers(host), event, quantity=5)
        placed = _buy(client, event, auth, ga).get_json()["data"]
        assert _tickets(client, event, auth)[0]["refundable"] is False
        response = client.post(f"/api/events/{event.id}/orders/{placed['id']}/refund", headers=auth)
        assert response.status_code == 409
        assert "closed" in response.get_json()["message"]


def test_someone_else_cannot_refund_my_order(db, client, auth_headers):
    host, buyer, other = make_user(db, seller=True), make_user(db), make_user(db)
    event = _event(db, host)
    ga = _add_type(client, auth_headers(host), event, quantity=5)
    placed = _buy(client, event, auth_headers(buyer), ga).get_json()["data"]
    response = client.post(f"/api/events/{event.id}/orders/{placed['id']}/refund",
                           headers=auth_headers(other))
    assert response.status_code == 404


def test_a_checked_in_ticket_cannot_be_refunded(db, client, auth_headers):
    host, buyer = make_user(db, seller=True), make_user(db)
    event = _event(db, host)
    ga = _add_type(client, auth_headers(host), event, quantity=5)
    placed = _buy(client, event, auth_headers(buyer), ga).get_json()["data"]
    code = _tickets(client, event, auth_headers(buyer))[0]["code"]
    client.post(f"/api/events/{event.id}/tickets/{code}/check-in", headers=auth_headers(host))
    response = client.post(f"/api/events/{event.id}/orders/{placed['id']}/refund",
                           headers=auth_headers(buyer))
    assert response.status_code == 409


# --- the waitlist ------------------------------------------------------------------------------

def _sold_out_event(db, client, auth_headers, quantity=2, **kw):
    host, first = make_user(db, seller=True), make_user(db)
    event = _event(db, host, **kw)
    ga = _add_type(client, auth_headers(host), event, quantity=quantity, perOrderLimit=4)
    placed = _buy(client, event, auth_headers(first), ga, quantity).get_json()["data"]
    return host, first, event, ga, placed


def test_the_waitlist_opens_when_sold_out_and_a_refund_holds_tickets_for_the_first_in_line(
    db, client, auth_headers
):
    host, first, event, ga, placed = _sold_out_event(db, client, auth_headers)
    alice, bob = make_user(db), make_user(db)

    assert _detail(client, event, auth_headers(alice))["waitlist"]["open"] is True
    joined = client.post(f"/api/events/{event.id}/waitlist",
                         json={"items": [{"ticketTypeId": ga, "quantity": 1}]},
                         headers=auth_headers(alice))
    assert joined.status_code == 200, joined.get_json()
    assert joined.get_json()["data"]["wanted"] == [{"ticketTypeId": ga, "quantity": 1}]
    client.post(f"/api/events/{event.id}/waitlist",
                json={"items": [{"ticketTypeId": ga, "quantity": 2}]}, headers=auth_headers(bob))
    assert _detail(client, event, auth_headers(host))["waitlist"]["waitingCount"] == 2

    # The first buyer gives both tickets back: Alice (first in line) gets the 1 she asked for,
    # Bob gets the other — "the rest go to the next people in line at the same time".
    assert client.post(f"/api/events/{event.id}/orders/{placed['id']}/refund",
                       headers=auth_headers(first)).status_code == 200
    alice_view = _detail(client, event, auth_headers(alice))["waitlist"]
    bob_view = _detail(client, event, auth_headers(bob))["waitlist"]
    assert alice_view["held"]["items"] == [{"ticketTypeId": ga, "quantity": 1}]
    assert bob_view["held"]["items"] == [{"ticketTypeId": ga, "quantity": 1}]
    assert len(_notes(db, alice, "waitlist_hold")) == 1
    assert "held for you until" in _notes(db, alice, "waitlist_hold")[0].payload["message"]

    # Held tickets are theirs: nobody else can buy them, Alice can.
    stranger = make_user(db)
    assert _buy(client, event, auth_headers(stranger), ga).status_code == 409
    bought = _buy(client, event, auth_headers(alice), ga)
    assert bought.status_code == 201, bought.get_json()
    entry = db.execute(select(WaitlistEntry).where(WaitlistEntry.user_id == alice.id)).scalar_one()
    assert entry.status == WAITLIST_BOUGHT


def test_joining_is_refused_while_tickets_are_on_sale(db, client, auth_headers):
    host, someone = make_user(db, seller=True), make_user(db)
    event = _event(db, host)
    ga = _add_type(client, auth_headers(host), event, quantity=5)
    response = client.post(f"/api/events/{event.id}/waitlist",
                           json={"items": [{"ticketTypeId": ga, "quantity": 1}]},
                           headers=auth_headers(someone))
    assert response.status_code == 409
    assert "waitlist" not in _detail(client, event, auth_headers(someone))


def test_an_unused_hold_passes_on_and_takes_them_off_the_waitlist(db, client, auth_headers):
    host, first, event, ga, placed = _sold_out_event(db, client, auth_headers, quantity=1)
    alice, bob = make_user(db), make_user(db)
    for person in (alice, bob):
        client.post(f"/api/events/{event.id}/waitlist",
                    json={"items": [{"ticketTypeId": ga, "quantity": 1}]}, headers=auth_headers(person))
    client.post(f"/api/events/{event.id}/orders/{placed['id']}/refund", headers=auth_headers(first))

    db.execute(update(WaitlistHold).where(WaitlistHold.user_id == alice.id)
               .values(held_until=_now() - timedelta(seconds=1)))
    db.commit()
    waitlist_service.expire()

    db.expire_all()
    assert db.execute(select(WaitlistEntry.status).where(WaitlistEntry.user_id == alice.id)).scalar() \
        == WAITLIST_EXPIRED
    assert len(_notes(db, alice, "waitlist_hold_ended")) == 1
    bob_hold = db.execute(select(WaitlistHold).where(WaitlistHold.user_id == bob.id)).scalar_one()
    assert bob_hold.status == HOLD_ACTIVE


def test_keep_waiting_gives_up_the_held_type_and_keeps_the_place_for_the_other(db, client, auth_headers):
    host, buyer = make_user(db, seller=True), make_user(db)
    event = _event(db, host)
    hauth = auth_headers(host)
    ga = _add_type(client, hauth, event, name="GA", quantity=1)
    vip = _add_type(client, hauth, event, name="VIP", quantity=1)
    ga_order = _buy(client, event, auth_headers(buyer), ga).get_json()["data"]
    _buy(client, event, auth_headers(buyer), vip)
    alice, bob = make_user(db), make_user(db)
    client.post(f"/api/events/{event.id}/waitlist", headers=auth_headers(alice),
                json={"items": [{"ticketTypeId": ga, "quantity": 1}, {"ticketTypeId": vip, "quantity": 1}]})
    client.post(f"/api/events/{event.id}/waitlist", headers=auth_headers(bob),
                json={"items": [{"ticketTypeId": ga, "quantity": 1}]})
    client.post(f"/api/events/{event.id}/orders/{ga_order['id']}/refund", headers=auth_headers(buyer))

    released = client.post(f"/api/events/{event.id}/waitlist/hold/release", headers=auth_headers(alice))
    assert released.status_code == 200, released.get_json()
    assert released.get_json()["data"]["wanted"] == [{"ticketTypeId": vip, "quantity": 1}]
    assert released.get_json()["data"]["held"] is None
    bob_view = _detail(client, event, auth_headers(bob))["waitlist"]
    assert bob_view["held"]["items"] == [{"ticketTypeId": ga, "quantity": 1}]


def test_more_tickets_from_the_host_go_to_the_line_first_and_the_rest_on_sale(db, client, auth_headers):
    host, first, event, ga, _ = _sold_out_event(db, client, auth_headers, quantity=1)
    alice = make_user(db)
    client.post(f"/api/events/{event.id}/waitlist",
                json={"items": [{"ticketTypeId": ga, "quantity": 2}]}, headers=auth_headers(alice))

    client.patch(f"/api/events/{event.id}/ticket-types/{ga}", json={"quantity": 6},
                 headers=auth_headers(host))

    held = _detail(client, event, auth_headers(alice))["waitlist"]["held"]
    assert held["items"] == [{"ticketTypeId": ga, "quantity": 2}]
    left = {t["id"]: t for t in _detail(client, event)["ticketTypes"]}[ga]["left"]
    assert left == 3, "6 - 1 sold - 2 held for Alice"


def test_the_hold_length_follows_how_soon_the_event_starts():
    class E:
        def __init__(self, hours):
            self.starts_at = _now() + timedelta(hours=hours)

    assert waitlist_service.hold_length(E(48)) == timedelta(hours=2)
    assert waitlist_service.hold_length(E(5)) == timedelta(minutes=30)
    assert waitlist_service.hold_length(E(1)) == timedelta(minutes=10)


def test_the_waitlist_closes_quietly_when_the_event_starts(db, client, auth_headers):
    host, first, event, ga, _ = _sold_out_event(db, client, auth_headers, quantity=1)
    alice = make_user(db)
    client.post(f"/api/events/{event.id}/waitlist",
                json={"items": [{"ticketTypeId": ga, "quantity": 1}]}, headers=auth_headers(alice))
    event.starts_at = _now() - timedelta(minutes=1)
    db.commit()
    waitlist_service.expire()
    db.expire_all()
    assert db.execute(select(WaitlistEntry.status)).scalar() == "closed"
    assert _notes(db, alice, "waitlist_hold_ended") == []


# --- check-in ----------------------------------------------------------------------------------

def _ticketed(db, client, auth_headers, quantity=2):
    host, buyer = make_user(db, seller=True), make_user(db, name="Ana Ruiz")
    event = _event(db, host)
    ga = _add_type(client, auth_headers(host), event, quantity=10)
    _buy(client, event, auth_headers(buyer), ga, quantity)
    return host, buyer, event, _tickets(client, event, auth_headers(buyer))


def test_a_ticket_checks_in_once_and_says_who_and_which(db, client, auth_headers):
    host, buyer, event, tickets = _ticketed(db, client, auth_headers)
    url = f"/api/events/{event.id}/tickets/{tickets[0]['code']}/check-in"

    first = client.post(url, headers=auth_headers(host)).get_json()["data"]
    again = client.post(url, headers=auth_headers(host)).get_json()["data"]

    assert first["result"] == "checked_in"
    assert first["ticket"]["holderName"] == "Ana Ruiz" and first["ticket"]["position"] == 1
    assert first["ticket"]["of"] == 2 and first["checkedIn"] == 1 and first["total"] == 2
    assert again["result"] == "already_checked_in" and "Already checked in at" in again["message"]


def test_check_in_by_number_and_the_guest_list(db, client, auth_headers):
    host, buyer, event, tickets = _ticketed(db, client, auth_headers)
    by_number = client.post(f"/api/events/{event.id}/tickets/{tickets[1]['number'].lower()}/check-in",
                            headers=auth_headers(host)).get_json()["data"]
    assert by_number["result"] == "checked_in"
    guests = client.get(f"/api/events/{event.id}/guests?q=ana", headers=auth_headers(host)).get_json()["data"]
    assert len(guests["guests"]) == 2 and guests["checkedIn"] == 1 and guests["ageLimit"] == "all"


def test_invalid_codes_and_who_may_scan(db, client, auth_headers):
    host, buyer, event, tickets = _ticketed(db, client, auth_headers)
    other_event = _event(db, host)
    wrong = client.post(f"/api/events/{other_event.id}/tickets/{tickets[0]['code']}/check-in",
                        headers=auth_headers(host)).get_json()["data"]
    assert wrong["result"] == "invalid" and "different event" in wrong["message"]
    forged = client.post(f"/api/events/{event.id}/tickets/S3T1.AAAA/check-in",
                         headers=auth_headers(host)).get_json()["data"]
    assert forged["result"] == "invalid"

    stranger = make_user(db)
    assert client.post(f"/api/events/{event.id}/tickets/{tickets[0]['code']}/check-in",
                       headers=auth_headers(stranger)).status_code == 404
    cohost = make_user(db)
    make_event_participant(db, event, cohost, role="cohost")
    assert client.post(f"/api/events/{event.id}/tickets/{tickets[0]['code']}/check-in",
                       headers=auth_headers(cohost)).get_json()["data"]["result"] == "checked_in"


def test_offline_scans_sync_and_the_earliest_scan_wins(db, client, auth_headers):
    host, buyer, event, tickets = _ticketed(db, client, auth_headers, quantity=1)
    cohost = make_user(db)
    make_event_participant(db, event, cohost, role="cohost")
    code = tickets[0]["code"]
    later = (_now() - timedelta(minutes=5)).isoformat()
    earlier = (_now() - timedelta(minutes=10)).isoformat()

    a = client.post(f"/api/events/{event.id}/check-in/sync", headers=auth_headers(host),
                    json={"scans": [{"code": code, "scannedAt": later}]}).get_json()["data"]
    b = client.post(f"/api/events/{event.id}/check-in/sync", headers=auth_headers(cohost),
                    json={"scans": [{"code": code, "scannedAt": earlier}]}).get_json()["data"]

    assert a["results"][0]["result"] == "checked_in"
    assert b["results"][0]["result"] == "checked_in", "the earlier scan wins"
    db.expire_all()
    ticket = db.get(Ticket, uuid.UUID(tickets[0]["id"]))
    assert ticket.checked_in_by_id == cohost.id
    again = client.post(f"/api/events/{event.id}/tickets/{code}/check-in",
                        headers=auth_headers(host)).get_json()["data"]
    assert again["result"] == "already_checked_in"


def test_two_doors_scanning_one_code_at_once_let_it_in_once(db, client, auth_headers, app):
    host, buyer, event, tickets = _ticketed(db, client, auth_headers, quantity=1)
    url = f"/api/events/{event.id}/tickets/{tickets[0]['code']}/check-in"
    headers = auth_headers(host)

    def scan():
        with app.test_client() as c:
            return c.post(url, headers=headers).get_json()["data"]["result"]

    results = run_concurrently(scan, scan)
    assert sorted(results) == ["already_checked_in", "checked_in"]


def test_the_manifest_gives_a_scanner_what_it_needs_offline(db, client, auth_headers):
    host, buyer, event, tickets = _ticketed(db, client, auth_headers)
    manifest = client.get(f"/api/events/{event.id}/check-in/manifest",
                          headers=auth_headers(host)).get_json()["data"]
    assert manifest["publicKey"] and manifest["codePrefix"] == "S3T1."
    assert {t["number"] for t in manifest["tickets"]} == {t["number"] for t in tickets}


# --- host cancels -------------------------------------------------------------------------------

def test_cancelling_refunds_everyone_voids_tickets_clears_the_line_and_bills_the_host_the_fees(
    db, client, auth_headers, stripe_enabled
):
    host, buyer = make_user(db, seller=True), make_user(db)
    event = _event(db, host)
    ga = _add_type(client, auth_headers(host), event, quantity=2)
    placed = _buy(client, event, auth_headers(buyer), ga, 2).get_json()["data"]
    order = db.get(TicketOrder, uuid.UUID(placed["id"]))
    stripe_enabled.add_charge("ch_cancel", fee_cents=175, amount_cents=order.total_cents)
    post_webhook(client, make_stripe_event("payment_intent.succeeded", {
        "id": order.payment_reference, "latest_charge": "ch_cancel", "amount": order.total_cents,
        "amount_received": order.total_cents,
        "metadata": {"kind": "ticket_order", "ticket_order_id": placed["id"]},
    }))
    waiting = make_user(db)
    client.post(f"/api/events/{event.id}/waitlist", headers=auth_headers(waiting),
                json={"items": [{"ticketTypeId": ga, "quantity": 1}]})

    preview = client.get(f"/api/events/{event.id}/cancel-preview", headers=auth_headers(host)).get_json()["data"]
    assert preview == {"ticketsToVoid": 2, "ordersToRefund": 1, "refundTotalCents": order.total_cents,
                       "cardFeesCents": 175, "waitlistCount": 1, "guestsToNotify": 1}

    cancelled = client.post(f"/api/events/{event.id}/cancel", json={"message": "Venue flooded."},
                            headers=auth_headers(host))
    assert cancelled.status_code == 200, cancelled.get_json()

    db.expire_all()
    assert db.get(TicketOrder, order.id).status == ORDER_REFUNDED
    assert [r["amount"] for r in stripe_enabled.refunds] == [order.total_cents]
    assert {t.status for t in db.query(Ticket).filter_by(order_id=order.id)} == {TICKET_REFUNDED}
    assert db.execute(select(WaitlistEntry.status)).scalar() == "closed"
    debt = db.execute(select(HostFeeDebt)).scalar_one()
    assert debt.amount_cents == 175 and debt.status == DEBT_OPEN
    note = _notes(db, buyer, "event_cancelled")[0]
    assert "Venue flooded." in note.payload["message"]
    assert len(_notes(db, waiting, "event_cancelled")) == 1
    assert_ledger_balanced(db)


def test_a_payment_landing_after_the_event_was_cancelled_is_refunded(db, client, auth_headers, stripe_enabled):
    host, buyer = make_user(db, seller=True), make_user(db)
    event = _event(db, host)
    ga = _add_type(client, auth_headers(host), event, quantity=2)
    placed = _buy(client, event, auth_headers(buyer), ga).get_json()["data"]
    order = db.get(TicketOrder, uuid.UUID(placed["id"]))
    client.post(f"/api/events/{event.id}/cancel", headers=auth_headers(host))
    stripe_enabled.add_charge("ch_late", fee_cents=80, amount_cents=order.total_cents)

    post_webhook(client, make_stripe_event("payment_intent.succeeded", {
        "id": order.payment_reference, "latest_charge": "ch_late", "amount": order.total_cents,
        "amount_received": order.total_cents,
        "metadata": {"kind": "ticket_order", "ticket_order_id": placed["id"]},
    }))

    db.expire_all()
    assert db.query(Ticket).filter_by(order_id=order.id).count() == 0
    assert [r["payment_intent"] for r in stripe_enabled.refunds] == [order.payment_reference]
    assert_ledger_balanced(db)


def test_an_event_with_tickets_cannot_be_deleted(db, client, auth_headers):
    host, buyer = make_user(db, seller=True), make_user(db)
    event = _event(db, host)
    ga = _add_type(client, auth_headers(host), event, priceCents=0)
    _buy(client, event, auth_headers(buyer), ga)
    response = client.delete(f"/api/events/{event.id}", headers=auth_headers(host))
    assert response.status_code == 409 and "Cancel it instead" in response.get_json()["message"]


# --- reschedule ---------------------------------------------------------------------------------

def test_rescheduling_tells_ticket_holders_once_and_opens_refunds_until_the_new_start(
    db, client, auth_headers
):
    host, buyer = make_user(db, seller=True), make_user(db)
    event = _event(db, host, refund_policy="none")
    ga = _add_type(client, auth_headers(host), event, quantity=5)
    _buy(client, event, auth_headers(buyer), ga)
    ticket = db.execute(select(Ticket)).scalar_one()
    before = ticket.pass_updated_at
    new_start = event.starts_at + timedelta(days=7)

    response = client.post(f"/api/events/{event.id}/reschedule", headers=auth_headers(host), json={
        "startsAt": new_start.isoformat(), "endsAt": (new_start + timedelta(hours=3)).isoformat(),
        "message": "The gallery needs another week."})

    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["rescheduledAt"] and data["rescheduleMessage"] == "The gallery needs another week."
    assert data["refundableUntil"] == new_start.isoformat()
    assert len(_notes(db, buyer, "event_rescheduled")) == 1
    assert _tickets(client, event, auth_headers(buyer))[0]["refundable"] is True
    db.expire_all()
    assert db.get(Ticket, ticket.id).pass_updated_at > before, "wallet passes update"


def test_patching_a_published_events_date_points_at_reschedule(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = _event(db, host)
    response = client.patch(f"/api/events/{event.id}", headers=auth_headers(host),
                            json={"startsAt": (event.starts_at + timedelta(days=1)).isoformat()})
    assert response.status_code == 409 and "Reschedule" in response.get_json()["message"]


# --- host payouts -------------------------------------------------------------------------------

def test_the_host_is_paid_after_the_event_less_fees_owed_from_a_cancelled_one(
    db, client, auth_headers, stripe_enabled, monkeypatch
):
    host, buyer = make_user(db, seller=True), make_user(db)
    hauth = auth_headers(host)

    # An earlier event the host cancelled: they owe the card fees.
    earlier = _event(db, host)
    ga0 = _add_type(client, hauth, earlier, quantity=5)
    p0 = _buy(client, earlier, auth_headers(buyer), ga0).get_json()["data"]
    o0 = db.get(TicketOrder, uuid.UUID(p0["id"]))
    stripe_enabled.add_charge("ch_e0", fee_cents=109, amount_cents=o0.total_cents)
    post_webhook(client, make_stripe_event("payment_intent.succeeded", {
        "id": o0.payment_reference, "latest_charge": "ch_e0", "amount": o0.total_cents,
        "amount_received": o0.total_cents, "metadata": {"kind": "ticket_order", "ticket_order_id": p0["id"]},
    }))
    client.post(f"/api/events/{earlier.id}/cancel", headers=hauth)

    # The event that happens: 2 x $25 sold.
    event = _event(db, host)
    ga = _add_type(client, hauth, event, quantity=5)
    p1 = _buy(client, event, auth_headers(buyer), ga, 2).get_json()["data"]
    o1 = db.get(TicketOrder, uuid.UUID(p1["id"]))
    stripe_enabled.add_charge("ch_e1", fee_cents=175, amount_cents=o1.total_cents)
    post_webhook(client, make_stripe_event("payment_intent.succeeded", {
        "id": o1.payment_reference, "latest_charge": "ch_e1", "amount": o1.total_cents,
        "amount_received": o1.total_cents, "metadata": {"kind": "ticket_order", "ticket_order_id": p1["id"]},
    }))
    event.starts_at = _now() - timedelta(days=2)
    event.ends_at = _now() - timedelta(days=2) + timedelta(hours=3)
    db.commit()

    monkeypatch.setenv("EVENT_PAYOUT_DELAY_HOURS", "24")
    result = host_payouts.archive_and_pay()

    assert result == {"archived": 1, "paid": 1}
    payout = db.execute(select(EventPayout).where(EventPayout.event_id == event.id)).scalar_one()
    assert payout.status == EVENT_PAYOUT_RELEASED
    assert payout.owed_cents == 2 * 2300 and payout.debt_recovered_cents == 109
    assert payout.amount_cents == 4600 - 109
    transfer = [t for t in stripe_enabled.transfers if t["transfer_group"] == f"event_{event.id}"]
    assert [t["amount"] for t in transfer] == [4491]
    db.expire_all()
    assert db.execute(select(HostFeeDebt.status)).scalar() == DEBT_SETTLED
    assert seller_balance(db, host.id) == 0
    assert_ledger_balanced(db)

    assert host_payouts.archive_and_pay() == {"archived": 0, "paid": 0}, "never twice"


def test_no_payout_until_a_day_after_the_event(db, client, auth_headers):
    host, buyer = make_user(db, seller=True), make_user(db)
    event = _event(db, host)
    ga = _add_type(client, auth_headers(host), event, quantity=5)
    _buy(client, event, auth_headers(buyer), ga)
    event.starts_at = _now() - timedelta(hours=4)
    event.ends_at = _now() - timedelta(hours=1)
    db.commit()
    assert host_payouts.archive_and_pay() == {"archived": 1, "paid": 0}
    assert db.execute(select(EventPayout)).first() is None


# --- B26: savers ---------------------------------------------------------------------------------

def test_savers_hear_once_when_a_sold_out_event_reopens(db, client, auth_headers):
    host, first, event, ga, placed = _sold_out_event(db, client, auth_headers, quantity=1)
    saver, going = make_user(db), first
    for person in (saver, going):
        db.add(EventSave(id=uuid.uuid4(), event_id=event.id, user_id=person.id))
    db.commit()
    db.expire_all()
    event = db.get(type(event), event.id)
    assert event.sold_out_at is not None
    event.sold_out_at = _now() - timedelta(hours=1)  # sold out a while ago, not a lapsed checkout
    db.commit()

    client.patch(f"/api/events/{event.id}/ticket-types/{ga}", json={"quantity": 3},
                 headers=auth_headers(host))

    assert len(_notes(db, saver, "event_tickets_available")) == 1
    assert _notes(db, going, "event_tickets_available") == [], "people going aren't told"


def test_a_free_rsvp_given_back_goes_to_the_waitlist(db, client, auth_headers):
    host, guest = make_user(db, seller=True), make_user(db)
    event = _event(db, host)
    free = _add_type(client, auth_headers(host), event, priceCents=0, quantity=1)
    client.post(f"/api/events/{event.id}/rsvp", json={"going": True}, headers=auth_headers(guest))
    alice = make_user(db)
    client.post(f"/api/events/{event.id}/waitlist",
                json={"items": [{"ticketTypeId": free, "quantity": 1}]}, headers=auth_headers(alice))

    client.post(f"/api/events/{event.id}/rsvp", json={"going": False}, headers=auth_headers(guest))
    assert _detail(client, event, auth_headers(alice))["waitlist"]["held"]["items"] == [
        {"ticketTypeId": free, "quantity": 1}]
    reserved = client.post(f"/api/events/{event.id}/rsvp", json={"going": True}, headers=auth_headers(alice))
    assert reserved.status_code == 200, reserved.get_json()
    assert db.query(Ticket).filter(Ticket.holder_id == alice.id, Ticket.status != TICKET_VOID).count() == 1
