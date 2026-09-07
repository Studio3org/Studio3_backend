"""Order money math and the ledger postings for each money event.

Every commission/fee calculation lives here so the split is defined exactly once. The
ledger postings live here too, next to the math they encode, so a change to the fee model
is a single-file change.
"""
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from src.shared.config.stripe_client import commission_bps, platform_currency
from src.shared.ledger import ledger_service
from src.shared.ledger.ledger_service import get_platform_account, get_seller_account
from src.shared.models.ledger import (
    ACCOUNT_PLATFORM_BANK,
    ACCOUNT_PLATFORM_CLEARING,
    ACCOUNT_PLATFORM_REVENUE,
    ACCOUNT_SHIPPING_COSTS,
    ACCOUNT_STRIPE_FEES,
    ACCOUNT_TAX_PAYABLE,
    CREDIT,
    DEBIT,
    TXN_ORDER_PAID,
    TXN_PAYOUT_RELEASED,
    TXN_REFUND_ISSUED,
    TXN_SHIPPING_COST_RECORDED,
    TXN_TRANSFER_REVERSED,
)
from src.shared.models.order import Order


def commission_cents(artwork_cents: int) -> int:
    """Platform commission, taken on the artwork price only — never on shipping or tax,
    which aren't the platform's to take a cut of."""
    return round(artwork_cents * commission_bps() / 10000)


def artist_share_cents(artwork_cents: int) -> int:
    return artwork_cents - commission_cents(artwork_cents)


def book_order_paid(
    db: Session,
    order: Order,
    stripe_fee_cents: int,
    commit: bool = True,
):
    """Book the money that arrived when a collector's payment succeeded.

        debit  platform_clearing   total - stripe_fee   (what actually landed in Stripe)
        debit  stripe_fees         stripe_fee           (the processing fee, absorbed)
        credit seller_payable      artwork - commission (owed to the artist, held)
        credit platform_revenue    commission + shipping
        credit tax_payable         tax                  (owed onward, not income)

    Debiting the full total would leave platform_clearing permanently out of step with the
    real Stripe balance, so the exact fee from the charge's balance_transaction is required
    here rather than an estimate.
    """
    artist = artist_share_cents(order.artwork_cents)
    commission = commission_cents(order.artwork_cents)
    currency = platform_currency().upper()

    entries = [
        (get_platform_account(db, ACCOUNT_PLATFORM_CLEARING, currency), DEBIT,
         order.total_cents - stripe_fee_cents),
        (get_platform_account(db, ACCOUNT_STRIPE_FEES, currency), DEBIT, stripe_fee_cents),
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
    artist = artist_share_cents(order.artwork_cents)
    commission = commission_cents(order.artwork_cents)

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
