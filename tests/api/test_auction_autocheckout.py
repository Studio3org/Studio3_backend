"""Phase 7b: no checkout after winning — shipping, insurance and tax charged to the winner's
saved card after the hammer price (the 9 Nov design), with Convelio faked as in Phase 8."""
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.modules.bids import auction_closer, holds_service
from src.shared.models.auction import AUCTION_AWAITING_WINNER, AUCTION_CLOSED_SOLD, Auction
from src.shared.models.notification import Notification
from src.shared.models.order import Order
from src.shared.models.piece import Piece
from src.shared.models.shipment import Shipment
from src.shared.models.shipping import ShippingQuote
from tests.convelio_fixtures import signed_webhook
from tests.factories import make_address, make_auction, make_bid, make_piece, make_user
from tests.helpers import assert_ledger_balanced


def _now():
    return datetime.now(timezone.utc)


def _won(db, stripe, *, delivery="ship"):
    artist = make_user(db, seller=True)
    studio = make_address(db, artist, city="Brooklyn", state="NY", zip="11201", phone="(718) 555-0100")
    artist.pickup_address_id = studio.id
    piece = make_piece(db, artist, price_cents=400_00, status="live", listing_type="auction",
                       package_length_cm=60, package_width_cm=50, package_height_cm=5, weight_kg=3)
    auction = make_auction(db, piece, artist, starting_bid_cents=400_00, soft_close_enabled=False,
                           opens_at=_now() - timedelta(days=7), closes_at=_now() - timedelta(minutes=1))
    auction.delivery_mode = delivery
    winner = make_user(db)
    ship_to = make_address(db, winner, city="Los Angeles", state="CA", zip="90049")
    card = stripe.add_card(holds_service.ensure_customer(db, winner))
    bid = make_bid(db, auction, winner, 500_00, with_hold=False, payment_method_id=card["id"])
    bid.address_id = ship_to.id
    db.commit()
    return artist, piece, auction, winner


def _notes(db, user, type_):
    return db.execute(select(Notification).where(Notification.user_id == user.id,
                                                 Notification.type == type_)).scalars().all()


def test_an_instant_quote_charges_shipping_and_tax_and_books_the_courier_with_no_checkout(
    db, stripe_enabled, convelio
):
    artist, piece, auction, winner = _won(db, stripe_enabled)

    auction_closer.close_expired_auctions()

    db.expire_all()
    auction = db.get(Auction, auction.id)
    assert auction.auto_checkout_state == "charged"
    order = db.get(Order, auction.checkout_order_id)
    assert order.status == "paid" and order.prepaid_cents == 500_00
    assert order.shipping_cents == 365_00, "Convelio's shipping and insurance"
    kinds = [(c["metadata"].get("kind"), c["amount"]) for c in stripe_enabled.charged]
    assert kinds == [("auction_charge", 500_00), ("auction_balance", order.total_cents - 500_00)]
    assert auction.status == AUCTION_CLOSED_SOLD
    assert db.get(Piece, piece.id).status == "sold"
    assert db.execute(select(Shipment.booking_state)).scalar() == "booked"
    assert len(_notes(db, winner, "auction_shipping_charged")) == 1
    assert_ledger_balanced(db)


def test_a_custom_quote_waits_then_charges_when_convelio_prices_it(db, client, stripe_enabled, convelio):
    convelio.mode = "processing"
    artist, piece, auction, winner = _won(db, stripe_enabled)

    auction_closer.close_expired_auctions()
    db.expire_all()
    waiting = db.get(Auction, auction.id)
    assert waiting.auto_checkout_state == "pending_quote" and waiting.status == AUCTION_AWAITING_WINNER
    assert len(_notes(db, winner, "auction_shipping_pending")) == 1
    assert len(stripe_enabled.charged) == 1, "only the hammer price so far"

    provider_id = db.get(ShippingQuote, waiting.shipping_quote_id).provider_quote_id
    signed_webhook(client, {"event": "custom_quote_ready", "created": "x", "payload": {"quote_id": provider_id}})

    db.expire_all()
    done = db.get(Auction, auction.id)
    assert done.auto_checkout_state == "charged" and done.status == AUCTION_CLOSED_SOLD
    assert _notes(db, winner, "shipping_quote_ready") == [], "no checkout to send them back to"
    assert_ledger_balanced(db)


def test_a_declined_balance_hands_back_to_the_auction_checkout(db, client, auth_headers, stripe_enabled, convelio):
    stripe_enabled.declined_kinds.add("auction_balance")
    artist, piece, auction, winner = _won(db, stripe_enabled)

    auction_closer.close_expired_auctions()

    db.expire_all()
    auction = db.get(Auction, auction.id)
    assert auction.auto_checkout_state == "failed" and auction.status == AUCTION_AWAITING_WINNER
    assert db.get(Piece, piece.id).status == "auction_won", "back to the winner"
    assert db.execute(select(Order.status)).scalar() == "cancelled"
    assert len(_notes(db, winner, "auction_shipping_failed")) == 1
    seen = client.get(f"/api/pieces/{piece.id}", headers=auth_headers(winner)).get_json()["data"]
    assert seen["autoCheckout"] == {"state": "failed", "orderId": None}
    assert_ledger_balanced(db)


def test_pickup_at_the_event_keeps_the_checkout(db, stripe_enabled, convelio):
    artist, piece, auction, winner = _won(db, stripe_enabled, delivery="pickup")

    auction_closer.close_expired_auctions()

    db.expire_all()
    assert db.get(Auction, auction.id).auto_checkout_state == "manual"
    assert convelio.quotes == [] and len(stripe_enabled.charged) == 1


def test_with_convelio_off_the_winner_checks_out_as_before(db, stripe_enabled, monkeypatch):
    monkeypatch.setenv("CONVELIO_ENABLED", "false")
    artist, piece, auction, winner = _won(db, stripe_enabled)

    auction_closer.close_expired_auctions()

    db.expire_all()
    assert db.get(Auction, auction.id).auto_checkout_state == "manual"
    assert db.execute(select(Order)).first() is None


def test_a_piece_missing_its_shipping_details_falls_back_to_the_checkout(db, stripe_enabled, convelio):
    artist, piece, auction, winner = _won(db, stripe_enabled)
    piece.weight_kg = None
    db.commit()

    auction_closer.close_expired_auctions()

    db.expire_all()
    assert db.get(Auction, auction.id).auto_checkout_state == "manual"
    assert json.dumps(convelio.quotes) == "[]"
    assert uuid.UUID(str(auction.id))
