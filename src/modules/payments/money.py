"""Order money math and the ledger postings for each money event.

Every commission/fee calculation lives here so the split is defined exactly once. The
ledger postings live here too, next to the math they encode, so a change to the fee model
is a single-file change.
"""

from sqlalchemy.orm import Session

from src.shared.config import commission as commission_policy
from src.shared.config.stripe_client import platform_currency
from src.shared.ledger import ledger_service
from src.shared.ledger.ledger_service import get_platform_account, get_seller_account
from src.shared.models.ledger import (
    ACCOUNT_AUCTION_ESCROW,
    ACCOUNT_PLATFORM_BANK,
    ACCOUNT_PLATFORM_CLEARING,
    ACCOUNT_PLATFORM_REVENUE,
    ACCOUNT_SHIPPING_COSTS,
    ACCOUNT_STRIPE_FEES,
    ACCOUNT_TAX_PAYABLE,
    CREDIT,
    DEBIT,
    TXN_AUCTION_CAPTURED,
    TXN_AUCTION_REFUNDED,
    TXN_ORDER_PAID,
    TXN_PAYOUT_RELEASED,
    TXN_REFUND_ISSUED,
    TXN_SHIPPING_COST_RECORDED,
    TXN_TRANSFER_REVERSED,
)
from src.shared.models.order import Order


def order_commission_bps(order: Order) -> int:
    """The rate this order was sold at.

    Always the snapshot stored on the row, never today's configured rate: a refund or payout
    recomputed from config would restate an order sold under a different tier.
    """
    return order.commission_bps


def commission_cents(artwork_cents: int, bps: int) -> int:
    """Platform commission, taken on the artwork price only — never on shipping or tax,
    which aren't the platform's to take a cut of.

    `bps` is required rather than looked up. Every caller either has an order, and therefore
    a snapshotted rate, or is quoting a hypothetical — and the two must not be confused.
    """
    return commission_policy.commission_cents(artwork_cents, bps)


def artist_share_cents(artwork_cents: int, bps: int) -> int:
    """What the artist receives. Commission comes out of the price, never on top of it."""
    return commission_policy.net_cents(artwork_cents, bps)


def book_order_paid(
    db: Session,
    order: Order,
    stripe_fee_cents: int,
    commit: bool = True,
):
    """Book the money that arrived when a collector's payment succeeded.

        debit  platform_clearing   balance - stripe_fee (what actually landed in Stripe now)
        debit  stripe_fees         stripe_fee           (the processing fee, absorbed)
        debit  auction_escrow      prepaid              (money already collected, cleared)
        credit seller_payable      artwork - commission (owed to the artist, held)
        credit platform_revenue    commission + shipping
        credit tax_payable         tax                  (owed onward, not income)

    Debiting the full total would leave platform_clearing permanently out of step with the
    real Stripe balance, so the exact fee from the charge's balance_transaction is required
    here rather than an estimate.

    `prepaid_cents` is what an auction win already collected when the close captured the
    winner's hold. That money reached the Stripe balance then, not now, and was booked into
    auction_escrow at the time — so here it is *cleared out of escrow* rather than debited to
    clearing a second time. Treating it as newly arrived would double-count the whole hammer
    price in the platform's asset accounts. `stripe_fee_cents` covers only this charge; the
    fee on the earlier capture was booked with it.
    """
    bps = order_commission_bps(order)
    artist = artist_share_cents(order.artwork_cents, bps)
    commission = commission_cents(order.artwork_cents, bps)
    currency = platform_currency().upper()
    prepaid = order.prepaid_cents or 0
    balance_cents = order.total_cents - prepaid

    entries = [
        (get_platform_account(db, ACCOUNT_PLATFORM_CLEARING, currency), DEBIT,
         balance_cents - stripe_fee_cents),
        (get_platform_account(db, ACCOUNT_STRIPE_FEES, currency), DEBIT, stripe_fee_cents),
        (get_platform_account(db, ACCOUNT_AUCTION_ESCROW, currency), DEBIT, prepaid),
        (get_seller_account(db, order.seller_id, currency), CREDIT, artist),
        (get_platform_account(db, ACCOUNT_PLATFORM_REVENUE, currency), CREDIT,
         commission + order.shipping_cents),
        (get_platform_account(db, ACCOUNT_TAX_PAYABLE, currency), CREDIT, order.tax_cents),
    ]
    return ledger_service.post_transaction(
        db,
        txn_type=TXN_ORDER_PAID,
        entries=entries,
        idempotency_key=f"order_paid:{order.id}",
        order_id=order.id,
        description=f"Payment captured for order {order.id}",
        commit=commit,
    )


def book_payout_released(db: Session, order: Order, amount_cents: int, commit: bool = True):
    """Money leaving the platform balance to the artist's Connect account. Clears what the
    seller_payable account said we owed."""
    entries = [
        (get_seller_account(db, order.seller_id), DEBIT, amount_cents),
        (get_platform_account(db, ACCOUNT_PLATFORM_CLEARING), CREDIT, amount_cents),
    ]
    return ledger_service.post_transaction(
        db,
        txn_type=TXN_PAYOUT_RELEASED,
        entries=entries,
        idempotency_key=f"payout_released:{order.id}",
        order_id=order.id,
        description=f"Payout released to artist for order {order.id}",
        commit=commit,
    )


def book_refund_issued(db: Session, order: Order, stripe_fee_cents: int, commit: bool = True):
    """Reverse of book_order_paid, with one deliberate asymmetry: **Stripe does not return
    its processing fee on a refund**. The stripe_fees debit therefore stands, and the
    platform ends up down by the fee. Crediting it back would make the books show money the
    platform doesn't have.
    """
    bps = order_commission_bps(order)
    artist = artist_share_cents(order.artwork_cents, bps)
    commission = commission_cents(order.artwork_cents, bps)

    entries = [
        (get_seller_account(db, order.seller_id), DEBIT, artist),
        (get_platform_account(db, ACCOUNT_PLATFORM_REVENUE), DEBIT,
         commission + order.shipping_cents),
        (get_platform_account(db, ACCOUNT_TAX_PAYABLE), DEBIT, order.tax_cents),
        (get_platform_account(db, ACCOUNT_PLATFORM_CLEARING), CREDIT,
         order.total_cents - stripe_fee_cents),
        # The fee the platform ate on a sale that ultimately refunded.
        (get_platform_account(db, ACCOUNT_STRIPE_FEES), CREDIT, stripe_fee_cents),
    ]
    return ledger_service.post_transaction(
        db,
        txn_type=TXN_REFUND_ISSUED,
        entries=entries,
        idempotency_key=f"refund_issued:{order.id}",
        order_id=order.id,
        description=f"Refund issued to collector for order {order.id}",
        commit=commit,
    )


def book_shipping_cost(
    db: Session, order: Order, actual_cost_cents: int, commit: bool = True
):
    """What the courier actually charged, paid from the platform's own funds outside
    Stripe — hence platform_bank, not platform_clearing. Without this the books only ever
    show the flat rate collected from the buyer and overstate platform revenue."""
    entries = [
        (get_platform_account(db, ACCOUNT_SHIPPING_COSTS), DEBIT, actual_cost_cents),
        (get_platform_account(db, ACCOUNT_PLATFORM_BANK), CREDIT, actual_cost_cents),
    ]
    return ledger_service.post_transaction(
        db,
        txn_type=TXN_SHIPPING_COST_RECORDED,
        entries=entries,
        idempotency_key=f"shipping_cost:{order.id}",
        order_id=order.id,
        description=f"Courier cost for order {order.id}",
        commit=commit,
    )


def book_transfer_reversed(
    db: Session, order: Order, amount_cents: int, reversal_id: str, commit: bool = True
):
    """A transfer that was clawed back after the fact — money returns to the platform
    balance and is owed to the artist again."""
    entries = [
        (get_platform_account(db, ACCOUNT_PLATFORM_CLEARING), DEBIT, amount_cents),
        (get_seller_account(db, order.seller_id), CREDIT, amount_cents),
    ]
    return ledger_service.post_transaction(
        db,
        txn_type=TXN_TRANSFER_REVERSED,
        entries=entries,
        idempotency_key=f"transfer_reversed:{reversal_id}",
        order_id=order.id,
        description=f"Transfer reversed for order {order.id}",
        commit=commit,
    )


def book_auction_captured(
    db: Session,
    auction,
    hold,
    stripe_fee_cents: int,
    commit: bool = True,
):
    """Book the hammer price taken when an auction closed on a winner.

        debit  platform_clearing   amount - stripe_fee
        debit  stripe_fees         stripe_fee
        credit auction_escrow      amount

    This happens before any order exists, which is exactly why it needs its own posting. The
    alternative — waiting for checkout — meant a winner who never completed left real money
    in the Stripe balance with no ledger record of it at all.

    Escrow is a liability, not income: the platform is holding the money for a sale that has
    not completed yet. It is cleared by book_order_paid, or given back by
    book_auction_refunded.
    """
    currency = platform_currency().upper()
    entries = [
        (get_platform_account(db, ACCOUNT_PLATFORM_CLEARING, currency), DEBIT,
         hold.amount_cents - stripe_fee_cents),
        (get_platform_account(db, ACCOUNT_STRIPE_FEES, currency), DEBIT, stripe_fee_cents),
        (get_platform_account(db, ACCOUNT_AUCTION_ESCROW, currency), CREDIT, hold.amount_cents),
    ]
    return ledger_service.post_transaction(
        db,
        txn_type=TXN_AUCTION_CAPTURED,
        entries=entries,
        # Keyed on the hold, not the auction: a cascade captures more than one hold against
        # the same auction, and they are separate movements of real money.
        idempotency_key=f"auction_captured:{hold.id}",
        description=f"Hammer price captured for auction {auction.id}",
        commit=commit,
    )


def book_auction_refunded(db: Session, auction, hold, commit: bool = True):
    """Give a captured hammer price back — a forfeited winner, or a sale that cannot complete.

        debit  auction_escrow      amount
        credit platform_clearing   amount

    The same asymmetry as book_refund_issued applies: Stripe does not return its processing
    fee on a refund, so the stripe_fees debit booked at capture stands and the platform is
    down by that fee. Crediting it back would show money the platform does not have.
    """
    currency = platform_currency().upper()
    entries = [
        (get_platform_account(db, ACCOUNT_AUCTION_ESCROW, currency), DEBIT, hold.amount_cents),
        (get_platform_account(db, ACCOUNT_PLATFORM_CLEARING, currency), CREDIT, hold.amount_cents),
    ]
    return ledger_service.post_transaction(
        db,
        txn_type=TXN_AUCTION_REFUNDED,
        entries=entries,
        idempotency_key=f"auction_refunded:{hold.id}",
        description=f"Hammer price refunded for auction {auction.id}",
        commit=commit,
    )
