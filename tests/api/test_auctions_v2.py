"""Phase 7a: auctions v2, the 9 Nov design (BACKEND_CHANGES B20; decisions A1, A2).

The saved card is charged at close; 12 hours to fix a failed card, with retries, and a window
that never moves (BE-4); then a second-chance offer to the next bidder; increments and a
fixed end time from the design; and PAY-3 (an abandoned auction checkout no longer strands
the winner)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from src.modules.bids import auction_closer, auction_dao, bid_dao, holds_service
from src.modules.orders import stale_orders
from src.shared.config.auction_config import bid_increment_cents
from src.shared.models.auction import (
    AUCTION_AWAITING_PAYMENT,
    AUCTION_AWAITING_WINNER,
    AUCTION_CLOSED_SOLD,
    AUCTION_NEEDS_SELLER_ACTION,
    AUCTION_SECOND_CHANCE,
    HOLD_CAPTURED,
    OFFER_PENDING,
    Auction,
    AuctionOffer,
    Hold,
)
from src.shared.models.notification import Notification
from src.shared.models.order import Order
from src.shared.models.piece import Piece
from tests.factories import make_address, make_auction, make_bid, make_piece, make_user
from tests.helpers import assert_ledger_balanced


def _now():
    return datetime.now(timezone.utc)


def _notes(db, user, type_):
    return db.execute(select(Notification).where(Notification.user_id == user.id,
                                                 Notification.type == type_)).scalars().all()


def _card(db, stripe, user):
    card = stripe.add_card(holds_service.ensure_customer(db, user))
    db.commit()
    return card["id"]


def _ended(db, *, reserve=None, starting=300_00):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=starting, status="live", listing_type="auction")
    auction = make_auction(db, piece, seller, starting_bid_cents=starting, reserve_cents=reserve,
                           opens_at=_now() - timedelta(days=7), closes_at=_now() - timedelta(minutes=1),
                           soft_close_enabled=False)
    return seller, piece, auction


def _bid(db, stripe, auction, amount):
    bidder = make_user(db)
    bid = make_bid(db, auction, bidder, amount, with_hold=False,
                   payment_method_id=_card(db, stripe, bidder))
    db.commit()
    return bidder, bid


def _get(db, auction):
    db.expire_all()
    return db.get(Auction, auction.id)


# --- the charge at close -----------------------------------------------------------------------

def test_the_winners_saved_card_is_charged_at_close_and_nobody_else(db, stripe_enabled):
    seller, piece, auction = _ended(db)
    winner, win = _bid(db, stripe_enabled, auction, 450_00)
    loser, _ = _bid(db, stripe_enabled, auction, 400_00)

    auction_closer.close_expired_auctions()

    assert _get(db, auction).status == AUCTION_AWAITING_WINNER
    assert [(c["payment_method"], c["amount"]) for c in stripe_enabled.charged] == [
        (win.payment_method_id, 450_00)]
    assert stripe_enabled.charged[0]["metadata"]["kind"] == "auction_charge"
    hold = db.execute(select(Hold).where(Hold.bid_id == win.id)).scalar_one()
    assert hold.status == HOLD_CAPTURED, "recorded as a captured charge for checkout and refunds"
    assert "charged" in _notes(db, winner, "auction_won")[0].payload["message"]
    assert len(_notes(db, loser, "auction_lost")) == 1
    assert_ledger_balanced(db)


def test_a_declined_card_gets_12_hours_with_retries_and_the_window_never_moves(db, stripe_enabled):
    seller, piece, auction = _ended(db)
    winner, win = _bid(db, stripe_enabled, auction, 450_00)
    stripe_enabled.declined_methods.add(win.payment_method_id)

    auction_closer.close_expired_auctions()
    parked = _get(db, auction)
    assert parked.status == AUCTION_AWAITING_PAYMENT
    deadline = parked.winner_deadline_at
    assert timedelta(hours=11, minutes=59) < deadline - _now() <= timedelta(hours=12)
    assert timedelta(minutes=59) < parked.next_payment_retry_at - _now() <= timedelta(hours=1)
    assert len(_notes(db, winner, "auction_payment_failed")) == 1

    # The first automatic retry fails too: the deadline stays put (BE-4), the next retry is
    # lined up at 4 hours.
    # An hour passes: the whole window moves back by it and the 1-hour retry is due.
    deadline = deadline - timedelta(hours=1, minutes=1)
    db.execute(update(Auction).where(Auction.id == auction.id)
               .values(winner_deadline_at=deadline, next_payment_retry_at=_now() - timedelta(seconds=1)))
    db.commit()
    auction_closer.expire_winner_windows()
    retried = _get(db, auction)
    assert retried.winner_deadline_at == deadline
    assert retried.payment_attempts == 2
    assert retried.next_payment_retry_at > _now() + timedelta(hours=2)

    # The bank lets it through on the next one.
    stripe_enabled.declined_methods.clear()
    db.execute(update(Auction).where(Auction.id == auction.id)
               .values(next_payment_retry_at=_now() - timedelta(seconds=1)))
    db.commit()
    auction_closer.expire_winner_windows()
    assert _get(db, auction).status == AUCTION_AWAITING_WINNER
    assert_ledger_balanced(db)


def test_the_winner_fixes_it_with_another_card_and_the_window_doesnt_move(db, client, auth_headers,
                                                                        stripe_enabled):
    seller, piece, auction = _ended(db)
    winner, win = _bid(db, stripe_enabled, auction, 450_00)
    stripe_enabled.declined_methods.add(win.payment_method_id)
    auction_closer.close_expired_auctions()
    deadline = _get(db, auction).winner_deadline_at

    bad = _card(db, stripe_enabled, winner)
    stripe_enabled.declined_methods.add(bad)
    refused = client.post(f"/api/pieces/{piece.id}/auction/retry-payment",
                          json={"paymentMethodId": bad}, headers=auth_headers(winner))
    assert refused.status_code == 402
    assert _get(db, auction).winner_deadline_at == deadline, "BE-4: a retry never extends it"

    good = _card(db, stripe_enabled, winner)
    fixed = client.post(f"/api/pieces/{piece.id}/auction/retry-payment",
                        json={"paymentMethodId": good}, headers=auth_headers(winner))
    assert fixed.status_code == 200, fixed.get_json()
    assert _get(db, auction).status == AUCTION_AWAITING_WINNER


# --- second chance -------------------------------------------------------------------------------

def _past_window(db, stripe, auction):
    auction_closer.close_expired_auctions()
    db.execute(update(Auction).where(Auction.id == auction.id)
               .values(winner_deadline_at=_now() - timedelta(seconds=1), next_payment_retry_at=None))
    db.commit()
    auction_closer.expire_winner_windows()


def test_after_12_hours_the_next_bidder_is_offered_it_and_accepting_charges_them(
    db, client, auth_headers, stripe_enabled
):
    seller, piece, auction = _ended(db)
    winner, win = _bid(db, stripe_enabled, auction, 450_00)
    runner, second = _bid(db, stripe_enabled, auction, 400_00)
    stripe_enabled.declined_methods.add(win.payment_method_id)

    _past_window(db, stripe_enabled, auction)

    assert _get(db, auction).status == AUCTION_SECOND_CHANCE
    offer = db.execute(select(AuctionOffer)).scalar_one()
    assert (offer.bidder_id, offer.amount_cents, offer.status) == (runner.id, 400_00, OFFER_PENDING)
    assert len(_notes(db, runner, "auction_second_chance")) == 1
    assert len(_notes(db, winner, "auction_forfeited")) == 1
    seen = client.get(f"/api/pieces/{piece.id}", headers=auth_headers(runner)).get_json()["data"]
    assert seen["secondChanceOffer"]["amountCents"] == 400_00
    other = client.get(f"/api/pieces/{piece.id}", headers=auth_headers(winner)).get_json()["data"]
    assert other["secondChanceOffer"] is None, "only the person offered it sees the offer"

    accepted = client.post(f"/api/pieces/{piece.id}/auction/offer/accept", headers=auth_headers(runner))
    assert accepted.status_code == 200, accepted.get_json()
    done = _get(db, auction)
    assert done.status == AUCTION_AWAITING_WINNER and done.winning_bid_id == second.id
    assert [c["amount"] for c in stripe_enabled.charged] == [400_00]
    assert_ledger_balanced(db)


def test_declining_passes_it_on_and_nobody_left_goes_to_the_seller(db, client, auth_headers,
                                                                 stripe_enabled):
    seller, piece, auction = _ended(db)
    _, win = _bid(db, stripe_enabled, auction, 450_00)
    runner, _ = _bid(db, stripe_enabled, auction, 400_00)
    stripe_enabled.declined_methods.add(win.payment_method_id)
    _past_window(db, stripe_enabled, auction)

    declined = client.post(f"/api/pieces/{piece.id}/auction/offer/decline", headers=auth_headers(runner))
    assert declined.status_code == 200
    assert _get(db, auction).status == AUCTION_NEEDS_SELLER_ACTION
    assert len(_notes(db, seller, "auction_needs_action")) == 1
    assert stripe_enabled.charged == []
    db.expire_all()
    assert db.get(Piece, piece.id).status == "delisted"


def test_an_unanswered_offer_expires_to_the_next_bidder(db, stripe_enabled):
    seller, piece, auction = _ended(db)
    _, win = _bid(db, stripe_enabled, auction, 500_00)
    second, _ = _bid(db, stripe_enabled, auction, 450_00)
    third, _ = _bid(db, stripe_enabled, auction, 400_00)
    stripe_enabled.declined_methods.add(win.payment_method_id)
    _past_window(db, stripe_enabled, auction)

    db.execute(update(AuctionOffer).values(expires_at=_now() - timedelta(seconds=1)))
    db.commit()
    auction_closer.expire_winner_windows()

    pending = db.execute(select(AuctionOffer).where(AuctionOffer.status == OFFER_PENDING)).scalar_one()
    assert pending.bidder_id == third.id
    assert len(_notes(db, second, "auction_second_chance_expired")) == 1


def test_a_runner_up_below_the_reserve_is_never_offered_it(db, stripe_enabled):
    seller, piece, auction = _ended(db, reserve=450_00)
    _, win = _bid(db, stripe_enabled, auction, 500_00)
    _bid(db, stripe_enabled, auction, 400_00)
    stripe_enabled.declined_methods.add(win.payment_method_id)

    _past_window(db, stripe_enabled, auction)

    assert _get(db, auction).status == AUCTION_NEEDS_SELLER_ACTION
    assert db.execute(select(AuctionOffer)).first() is None


# --- the rules -----------------------------------------------------------------------------------

def test_the_increments_are_the_designs():
    assert [bid_increment_cents(c) for c in (100_00, 499_00, 500_00, 999_00, 1_000_00,
                                             4_999_00, 5_000_00, 9_999_00, 10_000_00)] == [
        25_00, 25_00, 50_00, 50_00, 100_00, 100_00, 250_00, 250_00, 500_00]


def test_a_new_auction_has_a_fixed_end_time(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, seller, price_cents=200_00, listing_type="auction")
    auction = auction_dao.create_auction(db, piece, seller, starting_bid_cents=200_00, duration_days=7)
    auction_dao.open_auction(db, auction)
    closing = _now() + timedelta(minutes=2)
    auction.closes_at = closing
    db.commit()

    bid_dao.place_bid(db, auction.id, bidder, 200_00, "pm_test_card")

    assert _get(db, auction).closes_at == closing, "no extensions (the 9 Nov design)"


def test_an_auction_from_before_7a_still_captures_its_hold(db, stripe_enabled):
    seller, piece, auction = _ended(db)
    old_winner = make_user(db)
    bid = make_bid(db, auction, old_winner, 400_00)  # with a held authorisation
    db.commit()

    auction_closer.close_expired_auctions()

    assert _get(db, auction).status == AUCTION_AWAITING_WINNER
    assert stripe_enabled.charged == [] and len(stripe_enabled.captures) == 1
    assert db.execute(select(Hold.status).where(Hold.bid_id == bid.id)).scalar() == HOLD_CAPTURED


# --- PAY-3 ---------------------------------------------------------------------------------------

def test_an_abandoned_auction_checkout_goes_back_to_the_winner(db, client, auth_headers, stripe_enabled):
    seller, piece, auction = _ended(db)
    winner, _ = _bid(db, stripe_enabled, auction, 450_00)
    auction_closer.close_expired_auctions()
    address = make_address(db, winner)

    first = client.post(f"/api/pieces/{piece.id}/auction-checkout", headers=auth_headers(winner),
                        json={"addressId": str(address.id), "shippingMethod": "standard"})
    assert first.status_code == 201, first.get_json()
    assert _get(db, auction).status == AUCTION_AWAITING_WINNER, "not sold until it's paid"

    db.execute(update(Order).values(created_at=_now() - timedelta(hours=1)))
    db.commit()
    stale_orders.expire_abandoned_orders()

    db.expire_all()
    assert db.get(Piece, piece.id).status == "auction_won", "back to the winner, not delisted"
    again = client.post(f"/api/pieces/{piece.id}/auction-checkout", headers=auth_headers(winner),
                        json={"addressId": str(address.id), "shippingMethod": "standard"})
    assert again.status_code == 201, again.get_json()


def test_paying_the_auction_checkout_marks_it_sold(db, client, auth_headers):
    """Dev mode (no Stripe): the confirm pays it, and only then is the auction sold."""
    seller, piece, auction = _ended(db)
    winner = make_user(db)
    make_bid(db, auction, winner, 450_00, with_hold=False, payment_method_id="pm_test_card")
    db.commit()
    auction_closer.close_expired_auctions()
    address = make_address(db, winner)
    order = client.post(f"/api/pieces/{piece.id}/auction-checkout", headers=auth_headers(winner),
                        json={"addressId": str(address.id), "shippingMethod": "standard"}).get_json()["data"]

    assert client.post(f"/api/orders/{order['id']}/confirm", headers=auth_headers(winner)).status_code == 200
    assert _get(db, auction).status == AUCTION_CLOSED_SOLD
