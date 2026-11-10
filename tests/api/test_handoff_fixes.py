"""Fixes from the frontend handoff audit (10 Nov): partly refunded orders keep their passes and
lock the refund policy, the phone's time zone never beats the location's, and Exhibited is
filed under the event's own month."""
import uuid
from datetime import datetime, timedelta, timezone

from src.modules.events import lineup_service
from src.modules.wallet import wallet_service
from src.shared.models.event import PieceExhibition
from src.shared.models.ticketing import ORDER_PARTLY_REFUNDED, TicketOrder
from tests.factories import make_event, make_piece, make_user
from tests.stripe_fixtures import make_event as make_stripe_event
from tests.stripe_fixtures import post_webhook


def _now():
    return datetime.now(timezone.utc)


def _paid_order(db, client, auth_headers, stripe, host, buyer, quantity=2):
    event = make_event(db, host, status="published", refund_policy="7d", starts_in=timedelta(days=14))
    ga = client.post(f"/api/events/{event.id}/ticket-types", headers=auth_headers(host),
                     json={"name": "GA", "priceCents": 2500, "quantity": 10}).get_json()["data"]["id"]
    placed = client.post(f"/api/events/{event.id}/orders", headers=auth_headers(buyer),
                         json={"items": [{"ticketTypeId": ga, "quantity": quantity}]}).get_json()["data"]
    order = db.get(TicketOrder, uuid.UUID(placed["id"]))
    stripe.add_charge("ch_fix", fee_cents=100, amount_cents=order.total_cents)
    post_webhook(client, make_stripe_event("payment_intent.succeeded", {
        "id": order.payment_reference, "latest_charge": "ch_fix", "amount": order.total_cents,
        "amount_received": order.total_cents,
        "metadata": {"kind": "ticket_order", "ticket_order_id": placed["id"]},
    }))
    return event, placed["id"]


def test_a_partly_refunded_order_keeps_its_passes_and_locks_the_refund_policy(
    db, client, auth_headers, stripe_enabled
):
    host, buyer = make_user(db, seller=True), make_user(db)
    event, order_id = _paid_order(db, client, auth_headers, stripe_enabled, host, buyer)
    tickets = client.get(f"/api/user/me/tickets?eventId={event.id}",
                         headers=auth_headers(buyer)).get_json()["data"]["tickets"]
    refunded = client.post(f"/api/events/{event.id}/orders/{order_id}/refund",
                           json={"ticketIds": [tickets[0]["id"]]}, headers=auth_headers(buyer))
    assert refunded.get_json()["data"]["status"] == ORDER_PARTLY_REFUNDED

    db.expire_all()
    order = db.get(TicketOrder, uuid.UUID(order_id))
    assert wallet_service._checked_order(db, order) is order, "the ticket still held keeps its pass"
    changed = client.patch(f"/api/events/{event.id}", headers=auth_headers(host),
                           json={"refundPolicy": "none"})
    assert changed.status_code == 409


def test_a_phone_time_zone_never_overrides_the_events_location(db, client, auth_headers):
    host = make_user(db, seller=True)
    start = _now() + timedelta(days=5)
    created = client.post("/api/events", headers=auth_headers(host), json={
        "title": "Austin show", "startsAt": start.isoformat(),
        "endsAt": (start + timedelta(hours=3)).isoformat(),
        "latitude": 30.2672, "longitude": -97.7431,
    }).get_json()["data"]
    patched = client.patch(f"/api/events/{created['id']}", headers=auth_headers(host),
                           json={"timezone": "America/New_York"})
    assert patched.get_json()["data"]["timezone"] == "America/Chicago"


def test_exhibited_is_filed_under_the_events_own_month(db):
    host = make_user(db, seller=True)
    piece = make_piece(db, host, price_cents=100_00, status="live")
    event = make_event(db, host, status="published")
    event.timezone = "America/Los_Angeles"
    # 7pm on 30 November in Los Angeles is already December in UTC.
    db.add(PieceExhibition(id=uuid.uuid4(), piece_id=piece.id, event_id=event.id, title="Late show",
                           starts_at=datetime(2026, 12, 1, 3, 0, tzinfo=timezone.utc)))
    db.commit()
    assert lineup_service.exhibitions_for_piece(db, piece.id)[0]["month"] == "November 2026"
