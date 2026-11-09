"""scripts/fix_be1_refund_ledger.py: corrects refunds booked with the fee treated as returned."""
import importlib.util
from pathlib import Path

from src.modules.payments import money
from src.shared.ledger import ledger_service
from src.shared.ledger.ledger_service import get_platform_account, get_seller_account
from src.shared.models.ledger import (
    ACCOUNT_PLATFORM_CLEARING,
    ACCOUNT_PLATFORM_REVENUE,
    ACCOUNT_STRIPE_FEES,
    ACCOUNT_TAX_PAYABLE,
    CREDIT,
    DEBIT,
    TXN_REFUND_ISSUED,
)
from tests.factories import make_order, make_piece, make_user
from tests.helpers import assert_ledger_balanced, platform_balance

FEE = 320
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fix_be1_refund_ledger.py"


def _script():
    spec = importlib.util.spec_from_file_location("fix_be1_refund_ledger", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _refund_booked_the_old_way(db):
    seller = make_user(db, seller=True)
    order = make_order(
        db, buyer=make_user(db), seller=seller, piece=make_piece(db, seller), status="refunded"
    )
    money.book_order_paid(db, order, stripe_fee_cents=FEE)
    bps = money.order_commission_bps(order)
    ledger_service.post_transaction(
        db,
        txn_type=TXN_REFUND_ISSUED,
        entries=[
            (get_seller_account(db, seller.id), DEBIT,
             money.artist_share_cents(order.artwork_cents, bps)),
            (get_platform_account(db, ACCOUNT_PLATFORM_REVENUE), DEBIT,
             money.commission_cents(order.artwork_cents, bps) + order.shipping_cents),
            (get_platform_account(db, ACCOUNT_TAX_PAYABLE), DEBIT, order.tax_cents),
            (get_platform_account(db, ACCOUNT_PLATFORM_CLEARING), CREDIT, order.total_cents - FEE),
            (get_platform_account(db, ACCOUNT_STRIPE_FEES), CREDIT, FEE),
        ],
        idempotency_key=f"refund_issued:{order.id}",
        order_id=order.id,
    )
    return order


def test_the_script_corrects_old_refunds_once_and_only_when_applied(db):
    order = _refund_booked_the_old_way(db)
    script = _script()
    assert platform_balance(db, ACCOUNT_STRIPE_FEES) == 0, "the bug: the fee vanished"

    assert script.affected(db) == [(order.id, FEE)]
    script.main(apply=False)
    assert platform_balance(db, ACCOUNT_STRIPE_FEES) == 0, "a dry run writes nothing"

    script.main(apply=True)
    script.main(apply=True)

    db.expire_all()
    assert platform_balance(db, ACCOUNT_STRIPE_FEES) == -FEE
    assert platform_balance(db, ACCOUNT_PLATFORM_CLEARING) == FEE
    assert_ledger_balanced(db)
