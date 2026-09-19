"""Closing an auction on a winner, and passing it down when one falls through.

The properties these tests exist to pin down, in order of how much money they are worth:

1. A winner's captured money is never taken twice — the hammer price is collected at close,
   so checkout may only charge the remainder.
2. The winner can actually be identified after the close.
3. A declined card cascades to the next bidder rather than voiding the sale.
4. Runners-up are released only once someone's money is genuinely captured.
"""
from datetime import datetime, timedelta, timezone

from src.modules.bids import auction_closer, auction_winner, bid_dao
from src.shared.models.auction import (
    AUCTION_AWAITING_PAYMENT,
    AUCTION_AWAITING_WINNER,
    AUCTION_NEEDS_SELLER_ACTION,
    HOLD_CAPTURED,
    HOLD_HELD,
    HOLD_RELEASED,
    Auction,
    Hold,
)
from src.shared.models.bid import BID_FORFEITED, BID_LOST, BID_WON, Bid
from src.shared.models.piece import Piece
from tests.factories import make_auction, make_bid, make_piece, make_user
from tests.helpers import assert_ledger_balanced


def _closed_auction(db, *, seller, reserve_cents=None, event_id=None):
    """An auction whose clock has already run out, ready for the sweep."""
    piece = make_piece(db, seller, price_cents=500_00, status="live", listing_type="auction")
    now = datetime.now(timezone.utc)
    auction = make_auction(
        db, piece, seller,
        starting_bid_cents=500_00,
        reserve_cents=reserve_cents,
        opens_at=now - timedelta(days=7),
        closes_at=now - timedelta(minutes=1),
        event_id=event_id,
    )
    return piece, auction


def _hold(db, bid) -> Hold:
    return db.query(Hold).filter_by(bid_id=bid.id).one()


# --- the winner can be found at all -------------------------------------------------------

def test_the_winner_is_readable_after_the_close(db):
    """Regression: the winner used to be derived as "the highest active bid".

    The close marks the winning bid `won`, which that query filters out — so the instant a
    winner was decided, the auction had no winner, and auction_checkout answered every real
    winner with "only the winning bidder can complete this purchase".
    """
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    piece, auction = _closed_auction(db, seller=seller)
    bid = make_bid(db, auction, buyer, 600_00)

    auction_closer.close_expired_auctions()

    db.expire_all()
    auction = db.get(Auction, auction.id)
    assert auction.status == AUCTION_AWAITING_WINNER
    assert auction.winning_bid_id == bid.id

    winner = auction_winner.winning_bid(db, auction)
    assert winner is not None, "the auction lost track of who won"
    assert winner.bidder_id == buyer.id
    assert db.get(Bid, bid.id).status == BID_WON
    assert db.get(Piece, piece.id).status == "auction_won"


def test_a_settled_close_captures_the_winner_and_releases_everyone_else(db):
    seller = make_user(db, seller=True)
    winner_user, loser = make_user(db), make_user(db)
    piece, auction = _closed_auction(db, seller=seller)
    losing_bid = make_bid(db, auction, loser, 550_00)
    winning_bid = make_bid(db, auction, winner_user, 700_00)

    auction_closer.close_expired_auctions()

    db.expire_all()
    assert _hold(db, winning_bid).status == HOLD_CAPTURED
    assert _hold(db, losing_bid).status == HOLD_RELEASED
    assert db.get(Bid, losing_bid.id).status == BID_LOST
    assert_ledger_balanced(db)


def test_the_capture_is_booked_to_escrow_before_any_order_exists(db):
    """Money taken at close has to be in the ledger immediately.

    Waiting for checkout to book it meant a winner who never completed left real money in
    the Stripe balance with no ledger record of it at all.
    """
    from src.shared.ledger.ledger_service import get_platform_account
    from src.shared.models.ledger import ACCOUNT_AUCTION_ESCROW, LedgerEntry

    seller = make_user(db, seller=True)
    buyer = make_user(db)
    piece, auction = _closed_auction(db, seller=seller)
    make_bid(db, auction, buyer, 800_00)

    auction_closer.close_expired_auctions()

    db.expire_all()
    escrow = get_platform_account(db, ACCOUNT_AUCTION_ESCROW, "USD")
    credited = sum(
        e.amount_cents
        for e in db.query(LedgerEntry).filter_by(account_id=escrow.id, direction="credit")
    )
    assert credited == 800_00, "the hammer price never reached the ledger"
    assert_ledger_balanced(db)


# --- the cascade --------------------------------------------------------------------------

def test_a_declined_winner_parks_for_payment_and_keeps_the_runners_up_funded(db, stripe_enabled):
    """The invariant the whole cascade rests on.

    Releasing the runners-up at close — which the first version did unconditionally — is what
    makes a cascade impossible: by the time the top bidder's card is known to have failed,
    the fallback has already had their money handed back.
    """
    seller = make_user(db, seller=True)
    top, runner_up = make_user(db), make_user(db)
    piece, auction = _closed_auction(db, seller=seller)
    runner_bid = make_bid(db, auction, runner_up, 550_00)
    top_bid = make_bid(db, auction, top, 700_00)
    stripe_enabled.fail_capture(_hold(db, top_bid).stripe_payment_intent_id)

    auction_closer.close_expired_auctions()

    db.expire_all()
    auction = db.get(Auction, auction.id)
    assert auction.status == AUCTION_AWAITING_PAYMENT
    assert auction.winning_bid_id == top_bid.id
    assert auction.winner_deadline_at is not None
    # The runner-up is still funded, which is the point.
    assert _hold(db, runner_bid).status == HOLD_HELD
    assert db.get(Bid, runner_bid.id).status != BID_LOST
    # And nobody has been charged.
    assert _hold(db, top_bid).status != HOLD_CAPTURED


def test_an_expired_window_passes_the_piece_to_the_next_bidder(db, stripe_enabled):
    seller = make_user(db, seller=True)
    top, runner_up = make_user(db), make_user(db)
    piece, auction = _closed_auction(db, seller=seller)
    runner_bid = make_bid(db, auction, runner_up, 550_00)
    top_bid = make_bid(db, auction, top, 700_00)
    stripe_enabled.fail_capture(_hold(db, top_bid).stripe_payment_intent_id)

    auction_closer.close_expired_auctions()
    # Their window runs out.
    db.expire_all()
    db.get(Auction, auction.id).winner_deadline_at = datetime.now(timezone.utc) - timedelta(
        minutes=1
    )
    db.commit()

    auction_closer.expire_winner_windows()

    db.expire_all()
    auction = db.get(Auction, auction.id)
    assert auction.status == AUCTION_AWAITING_WINNER
    assert auction.winning_bid_id == runner_bid.id
    assert auction.cascade_depth == 1
    assert db.get(Bid, top_bid.id).status == BID_FORFEITED
    assert _hold(db, top_bid).status == HOLD_RELEASED
    assert _hold(db, runner_bid).status == HOLD_CAPTURED
    assert_ledger_balanced(db)


def test_a_settled_sale_is_never_expired_by_the_sweep(db):
    """awaiting_winner means the money is already taken. There is no honest way to un-sell
    it on a timer, and the runners-up have been released so there is nothing to cascade to."""
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    piece, auction = _closed_auction(db, seller=seller)
    bid = make_bid(db, auction, buyer, 600_00)

    auction_closer.close_expired_auctions()
    db.expire_all()
    # Force a deadline into the past, as if one had been set.
    db.get(Auction, auction.id).winner_deadline_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    auction_closer.expire_winner_windows()

    db.expire_all()
    auction = db.get(Auction, auction.id)
    assert auction.status == AUCTION_AWAITING_WINNER
    assert auction.winning_bid_id == bid.id
    assert db.get(Bid, bid.id).status == BID_WON


def test_a_cascade_with_nobody_left_parks_for_the_seller(db, stripe_enabled):
    seller = make_user(db, seller=True)
    only = make_user(db)
    piece, auction = _closed_auction(db, seller=seller)
    bid = make_bid(db, auction, only, 600_00)
    stripe_enabled.fail_capture(_hold(db, bid).stripe_payment_intent_id)

    auction_closer.close_expired_auctions()
    db.expire_all()
    db.get(Auction, auction.id).winner_deadline_at = datetime.now(timezone.utc) - timedelta(
        minutes=1
    )
    db.commit()

    auction_closer.expire_winner_windows()

    db.expire_all()
    auction = db.get(Auction, auction.id)
    assert auction.status == AUCTION_NEEDS_SELLER_ACTION
    assert auction.winning_bid_id is None
    assert _hold(db, bid).status == HOLD_RELEASED
    # Nothing was charged to anyone, so the piece comes off the market rather than selling.
    assert db.get(Piece, piece.id).status == "delisted"


def test_the_cascade_stops_after_a_bounded_number_of_attempts(db):
    """An auction that has burned through four bidders has something wrong with it that a
    fifth automatic attempt will not fix. A human decides from there."""
    seller = make_user(db, seller=True)
    piece, auction = _closed_auction(db, seller=seller)
    for i in range(auction_winner.MAX_CASCADE_DEPTH + 2):
        make_bid(db, auction, make_user(db), 600_00 + i * 100_00)
    auction.cascade_depth = auction_winner.MAX_CASCADE_DEPTH
    db.commit()

    outcome = auction_winner.cascade(db, auction, piece, reason="test", commit=True)

    assert outcome == auction_winner.EXHAUSTED
    db.expire_all()
    assert db.get(Auction, auction.id).status == AUCTION_NEEDS_SELLER_ACTION


# --- windows ------------------------------------------------------------------------------

def test_an_event_auction_gets_the_short_window(db):
    """The client was explicit: at an event the room empties, so a failed payment is retried
    on the spot rather than two days later."""
    import uuid

    seller = make_user(db, seller=True)
    _, standalone = _closed_auction(db, seller=seller)
    _, event = _closed_auction(db, seller=seller, event_id=uuid.uuid4())

    assert auction_winner.retry_window(standalone) == auction_winner.RETRY_WINDOW_STANDALONE
    assert auction_winner.retry_window(event) == auction_winner.RETRY_WINDOW_EVENT
    assert auction_winner.RETRY_WINDOW_EVENT < auction_winner.RETRY_WINDOW_STANDALONE


# --- no-sale paths still work -------------------------------------------------------------

def test_a_reserve_that_is_not_met_charges_nobody(db):
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    piece, auction = _closed_auction(db, seller=seller, reserve_cents=900_00)
    bid = make_bid(db, auction, buyer, 600_00)

    auction_closer.close_expired_auctions()

    db.expire_all()
    assert db.get(Auction, auction.id).status == AUCTION_NEEDS_SELLER_ACTION
    assert _hold(db, bid).status == HOLD_RELEASED
    assert db.get(Auction, auction.id).winning_bid_id is None


def test_a_bid_with_no_hold_does_not_crash_the_close(db):
    """A bid that somehow has no money behind it cannot win, but it must not take the sweep
    down with it either — one bad auction must not stop the batch."""
    seller = make_user(db, seller=True)
    broken, good = make_user(db), make_user(db)
    piece, auction = _closed_auction(db, seller=seller)
    good_bid = make_bid(db, auction, good, 550_00)
    make_bid(db, auction, broken, 700_00, with_hold=False)

    auction_closer.close_expired_auctions()

    db.expire_all()
    auction = db.get(Auction, auction.id)
    assert auction.status == AUCTION_AWAITING_PAYMENT
    # The funded runner-up is untouched and available to cascade to.
    assert _hold(db, good_bid).status == HOLD_HELD


def test_min_next_bid_is_unaffected_by_settlement_fields(db):
    """Guard against the winner pointer leaking into the live bidding maths."""
    seller = make_user(db, seller=True)
    piece, auction = _closed_auction(db, seller=seller)
    assert bid_dao.min_next_bid_cents(db, auction) == auction.starting_bid_cents
