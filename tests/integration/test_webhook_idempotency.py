"""Webhook replay, dedup and signature handling.

Stripe retries deliveries and does not guarantee ordering, so these properties are what
stand between a transient network blip and an order that is paid at Stripe but
`pending_payment` forever in our database.
"""
import json

from sqlalchemy import func, select

from src.modules.payments import money
from src.shared.models.ledger import LedgerTransaction
from src.shared.models.order import Order
from src.shared.models.payout import Payout
from src.shared.models.webhook_event import StripeWebhookEvent
from tests.factories import make_order, make_piece, make_user
from tests.helpers import assert_ledger_balanced
from tests.stripe_fixtures import make_event, post_webhook, stripe_signature


def _paid_intent_event(order, charge_id="ch_test_1", event_id=None):
    return make_event(
        "payment_intent.succeeded",
        {
            "id": order.payment_reference or "pi_test_1",
            "latest_charge": charge_id,
            "metadata": {"order_id": str(order.id), "buyer_id": str(order.buyer_id)},
        },
        event_id=event_id,
    )


def _pending_order(db, *, artwork_cents=100_00, charge_id="ch_test_1"):
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    piece = make_piece(db, seller, price_cents=artwork_cents)
    order = make_order(
        db,
        buyer=buyer,
        seller=seller,
        piece=piece,
        artwork_cents=artwork_cents,
        payment_reference="pi_test_1",
    )
    return order


def test_event_that_failed_processing_is_reprocessed_on_retry(db, client, stripe_stub, monkeypatch):
    """Regression: a handler that raised must not make the event undeliverable.

    record_event committed the audit row before handle_event ran. When the handler failed,
    the row existed with processed_at NULL, and Stripe's retry hit the unique constraint and
    was answered 200 "duplicate" — so a single transient Charge.retrieve timeout left a paid
    order stuck in pending_payment with no ledger entry and no payout row, permanently.
    """
    order = _pending_order(db)
    stripe_stub.add_charge("ch_test_1", fee_cents=320, amount_cents=order.total_cents)
    event = _paid_intent_event(order)

    # First delivery: the handler blows up partway through.
    def _boom(charge_id, **kwargs):
        raise RuntimeError("Stripe timed out")

    monkeypatch.setattr(stripe_stub.Charge, "retrieve", _boom)
    first = post_webhook(client, event)
    assert first.status_code == 500

    db.expire_all()
    row = db.execute(
        select(StripeWebhookEvent).where(StripeWebhookEvent.stripe_event_id == event["id"])
    ).scalar_one()
    assert row.processed_at is None and row.error, "failure should be recorded, not silent"
    assert db.get(Order, order.id).status == "pending_payment"

    # Stripe retries the same event id. It must be processed this time, not dropped.
    monkeypatch.setattr(stripe_stub.Charge, "retrieve", stripe_stub._charge_retrieve)
    second = post_webhook(client, event)
    assert second.status_code == 200
    assert second.get_json().get("duplicate") is not True

    db.expire_all()
    assert db.get(Order, order.id).status == "paid"
    assert db.execute(
        select(func.count()).select_from(Payout.__table__).where(Payout.order_id == order.id)
    ).scalar_one() == 1
    assert_ledger_balanced(db)


def test_duplicate_of_a_processed_event_is_a_noop(db, client, stripe_stub):
    order = _pending_order(db)
    stripe_stub.add_charge("ch_test_1", fee_cents=320, amount_cents=order.total_cents)
    event = _paid_intent_event(order)

    first = post_webhook(client, event)
    second = post_webhook(client, event)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json()["duplicate"] is True

    db.expire_all()
    assert db.execute(
        select(func.count()).select_from(LedgerTransaction.__table__).where(
            LedgerTransaction.type == "order_paid"
        )
    ).scalar_one() == 1
    assert db.execute(
        select(func.count()).select_from(Payout.__table__).where(Payout.order_id == order.id)
    ).scalar_one() == 1
    assert_ledger_balanced(db)


def test_two_distinct_events_for_the_same_intent_post_the_ledger_once(db, client, stripe_stub):
    """Different event ids clear the dedup table, so the ledger's own idempotency_key is the
    only thing left holding the line."""
    order = _pending_order(db)
    stripe_stub.add_charge("ch_test_1", fee_cents=320, amount_cents=order.total_cents)

    post_webhook(client, _paid_intent_event(order, event_id="evt_one"))
    post_webhook(client, _paid_intent_event(order, event_id="evt_two"))

    db.expire_all()
    assert db.execute(
        select(func.count()).select_from(LedgerTransaction.__table__).where(
            LedgerTransaction.idempotency_key == f"order_paid:{order.id}"
        )
    ).scalar_one() == 1
    assert db.execute(
        select(func.count()).select_from(Payout.__table__).where(Payout.order_id == order.id)
    ).scalar_one() == 1
    assert_ledger_balanced(db)


def test_unhandled_event_type_is_recorded_then_ignored(db, client, stripe_stub):
    event = make_event("invoice.created", {"id": "in_test"})

    response = post_webhook(client, event)

    assert response.status_code == 200
    db.expire_all()
    row = db.execute(
        select(StripeWebhookEvent).where(StripeWebhookEvent.stripe_event_id == event["id"])
    ).scalar_one()
    assert row.processed_at is not None and row.error is None


def test_refund_of_a_completed_order_is_accepted(db, client, stripe_stub):
    """Regression: `completed` had no outgoing edge in VALID_TRANSITIONS, so a refund issued
    from the Stripe dashboard on a delivered order raised 409 inside the handler, returned
    500, and retried forever."""
    order = _pending_order(db, artwork_cents=200_00)
    order.status = "completed"
    order.stripe_charge_id = "ch_test_1"
    db.commit()
    money.book_order_paid(db, order, stripe_fee_cents=0)
    charge = stripe_stub.add_charge("ch_test_1", fee_cents=0, amount_cents=order.total_cents)

    response = post_webhook(client, make_event("charge.refunded", charge))

    assert response.status_code == 200
    db.expire_all()
    assert db.get(Order, order.id).status == "refunded"
    assert_ledger_balanced(db)


# --- signature verification (NFR-3) ----------------------------------------------------

def test_valid_signature_is_accepted(db, client, stripe_stub):
    response = post_webhook(client, make_event("invoice.created", {"id": "in_1"}))
    assert response.status_code == 200


def test_signature_from_the_second_configured_secret_is_accepted(db, client, stripe_stub):
    """Two endpoints (platform account and Connect) sign with different secrets; both are
    configured and construct_event loops over them."""
    response = post_webhook(
        client, make_event("invoice.created", {"id": "in_2"}), secret="whsec_test_connect"
    )
    assert response.status_code == 200


def test_tampered_body_is_rejected(db, client, stripe_stub):
    event = make_event("invoice.created", {"id": "in_3"})
    body = json.dumps(event).encode()
    signature = stripe_signature(body, "whsec_test_primary")

    response = post_webhook(
        client, event, signature=signature, payload=body.replace(b"in_3", b"in_4")
    )

    assert response.status_code == 400


def test_signature_from_an_unknown_secret_is_rejected(db, client, stripe_stub):
    response = post_webhook(
        client, make_event("invoice.created", {"id": "in_5"}), secret="whsec_not_ours"
    )
    assert response.status_code == 400


def test_missing_signature_header_is_rejected(db, client, stripe_stub):
    event = make_event("invoice.created", {"id": "in_6"})
    response = client.post(
        "/api/payments/webhook",
        data=json.dumps(event).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_nothing_is_recorded_when_the_signature_fails(db, client, stripe_stub):
    """An unverified payload must not reach the audit table — that is the whole point of
    verifying before recording."""
    post_webhook(client, make_event("invoice.created", {"id": "in_7"}), secret="whsec_not_ours")

    db.expire_all()
    assert db.execute(
        select(func.count()).select_from(StripeWebhookEvent.__table__)
    ).scalar_one() == 0
