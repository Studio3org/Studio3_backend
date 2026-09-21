"""Tiered commission, and the snapshot that keeps it honest over time.

The client's rates: 20% on original art, 15% for a named set of artists, 8% on event
tickets, all inclusive of Stripe's processing fee.
"""
import pytest

from src.modules.payments import money, payouts_service
from src.shared.config import commission as commission_policy
from src.shared.models.payout import PAYOUT_RELEASED
from tests.factories import make_order, make_payout, make_piece, make_user
from tests.helpers import assert_ledger_balanced, seller_balance


def _completed(db, seller, buyer, artwork_cents):
    piece = make_piece(db, seller, price_cents=artwork_cents, status="sold")
    order = make_order(db, buyer=buyer, seller=seller, piece=piece,
                       artwork_cents=artwork_cents, status="completed")
    money.book_order_paid(db, order, stripe_fee_cents=0)
    make_payout(db, order)
    return order


def test_commission_is_inclusive_not_added_on_top(db):
    """$100 of art at 20% means the buyer pays $100 and the artist receives $80."""
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    order = _completed(db, seller, buyer, 100_00)

    assert order.artwork_cents == 100_00
    assert order.total_cents == 100_00 + order.shipping_cents + order.tax_cents
    assert seller_balance(db, seller.id) == 80_00


def test_standard_art_rate_is_twenty_percent(db):
    seller = make_user(db, seller=True)
    assert commission_policy.resolve_bps(seller, commission_policy.SALE_ART) == 2000


def test_a_seller_override_gives_the_reduced_tier(db):
    """The client's lower rate for a named set of artists is a property of the seller, not
    of the listing — so it applies without anything being re-entered per piece."""
    standard = make_user(db, seller=True)
    favoured = make_user(db, seller=True, commission_bps_override=1500)
    buyer = make_user(db)

    standard_order = _completed(db, standard, buyer, 100_00)
    favoured_order = _completed(db, favoured, buyer, 100_00)

    assert standard_order.commission_bps == 2000
    assert favoured_order.commission_bps == 1500
    assert seller_balance(db, standard.id) == 80_00
    assert seller_balance(db, favoured.id) == 85_00
    assert_ledger_balanced(db)


def test_changing_the_platform_rate_does_not_restate_an_existing_order(db, monkeypatch, stripe_enabled):
    """The reason the rate is stored on the row at all.

    Reading it from config at payout time meant raising the platform rate would quietly
    increase the commission on every sale already made but not yet paid out.
    """
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    order = _completed(db, seller, buyer, 1000_00)
    assert order.commission_bps == 2000

    # The platform doubles its rate after the sale but before the payout.
    monkeypatch.setenv("PLATFORM_COMMISSION_BPS", "4000")
    assert commission_policy.default_bps(commission_policy.SALE_ART) == 4000

    result = payouts_service.release_payout_for_order(order.id)

    assert result["status"] == PAYOUT_RELEASED
    # 20% of $1000, the rate it was sold at — not 40%.
    assert result["amountCents"] == 800_00
    db.expire_all()
    assert seller_balance(db, seller.id) == 0
    assert_ledger_balanced(db)


def test_a_refund_reverses_at_the_original_rate(db):
    seller = make_user(db, seller=True, commission_bps_override=1500)
    buyer = make_user(db)
    order = _completed(db, seller, buyer, 200_00)
    assert seller_balance(db, seller.id) == 170_00

    money.book_refund_issued(db, order, stripe_fee_cents=0)

    db.expire_all()
    assert seller_balance(db, seller.id) == 0, "refund did not reverse the original split"
    assert_ledger_balanced(db)


def test_commission_is_never_taken_on_shipping_or_tax(db):
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    piece = make_piece(db, seller, price_cents=100_00, status="sold")
    order = make_order(db, buyer=buyer, seller=seller, piece=piece,
                       artwork_cents=100_00, shipping_cents=25_00, tax_cents=8_00,
                       status="completed")
    money.book_order_paid(db, order, stripe_fee_cents=0)

    # 20% of the artwork only; shipping and tax pass through untouched.
    assert seller_balance(db, seller.id) == 80_00
    assert_ledger_balanced(db)


# --- rate policy ------------------------------------------------------------------------

def test_ticket_rate_is_eight_percent():
    assert commission_policy.default_bps(commission_policy.SALE_TICKET) == 800


def test_the_ticket_rate_loses_money_on_small_tickets():
    """Surfaced deliberately rather than discovered in a month-end reconciliation: at 8%
    inclusive of Stripe's 2.9% + 30c, anything under about $5.89 costs the platform money."""
    assert not commission_policy.covers_stripe_fee(5_00, 800)
    assert commission_policy.covers_stripe_fee(6_00, 800)
    assert commission_policy.break_even_cents(800) == 589


def test_the_art_rate_comfortably_covers_the_stripe_fee():
    assert commission_policy.break_even_cents(2000) < 2_00
    assert commission_policy.covers_stripe_fee(10_00, 2000)


@pytest.mark.parametrize("raw,expected", [("20000", 10000), ("-5", 0), ("abc", 2000), ("", 2000)])
def test_a_misconfigured_rate_is_clamped_not_obeyed(monkeypatch, raw, expected):
    """A rate outside 0-100% is always a misconfiguration, and charging an artist a negative
    or greater-than-total commission is worse than ignoring the value."""
    monkeypatch.setenv("PLATFORM_COMMISSION_BPS", raw)
    assert commission_policy.default_bps(commission_policy.SALE_ART) == expected


def test_an_unknown_sale_kind_is_rejected():
    with pytest.raises(ValueError, match="Unknown sale kind"):
        commission_policy.default_bps("subscription")


def test_the_override_applies_to_art_not_tickets(db):
    """The reduced tier the client described is for art sales; event tickets keep the 8%."""
    seller = make_user(db, seller=True, commission_bps_override=1500)
    assert commission_policy.resolve_bps(seller, commission_policy.SALE_ART) == 1500
    assert commission_policy.resolve_bps(seller, commission_policy.SALE_TICKET) == 800


# --- the quote endpoint -----------------------------------------------------------------

def test_quote_matches_what_checkout_actually_books(db, client, auth_headers):
    """The whole point of a server-side quote: the number shown to the artist before they
    publish must be the number the ledger credits them afterwards."""
    seller = make_user(db, seller=True)
    buyer = make_user(db)

    quoted = client.get(
        "/api/user/me/commission-quote?priceCents=100000", headers=auth_headers(seller)
    ).get_json()["data"]

    _completed(db, seller, buyer, 1000_00)

    assert quoted["netCents"] == 800_00
    assert seller_balance(db, seller.id) == quoted["netCents"]


def test_quote_uses_the_sellers_own_rate(db, client, auth_headers):
    standard = make_user(db, seller=True)
    favoured = make_user(db, seller=True, commission_bps_override=1500)

    a = client.get("/api/user/me/commission-quote?priceCents=10000",
                   headers=auth_headers(standard)).get_json()["data"]
    b = client.get("/api/user/me/commission-quote?priceCents=10000",
                   headers=auth_headers(favoured)).get_json()["data"]

    assert (a["commissionBps"], a["netCents"]) == (2000, 80_00)
    assert (b["commissionBps"], b["netCents"]) == (1500, 85_00)


def test_quote_warns_when_the_sale_is_too_small_to_cover_processing(db, client, auth_headers):
    host = make_user(db, seller=True)

    cheap = client.get("/api/user/me/commission-quote?priceCents=500&saleKind=ticket",
                       headers=auth_headers(host)).get_json()["data"]
    fine = client.get("/api/user/me/commission-quote?priceCents=2000&saleKind=ticket",
                      headers=auth_headers(host)).get_json()["data"]

    assert cheap["coversProcessingFee"] is False
    assert cheap["breakEvenCents"] == 589
    assert fine["coversProcessingFee"] is True


@pytest.mark.parametrize("query,expected", [
    ("", 400), ("priceCents=abc", 400), ("priceCents=-100", 400),
    ("priceCents=1000&saleKind=subscription", 400),
])
def test_quote_rejects_bad_input(db, client, auth_headers, query, expected):
    seller = make_user(db, seller=True)
    assert client.get(
        f"/api/user/me/commission-quote?{query}", headers=auth_headers(seller)
    ).status_code == expected


def test_quote_requires_authentication(client):
    assert client.get("/api/user/me/commission-quote?priceCents=1000").status_code == 401
