"""Correct refunds booked before BE-1 was fixed. Dry run unless --apply.

Until Phase 2, every full refund was booked as if Stripe had returned its processing fee:
it credited `stripe_fees` with the fee and credited `platform_clearing` with the total minus
the fee. Stripe keeps the fee, so each of those refunds left clearing a fee higher than the
real Stripe balance and the fee expense a fee too low.

For each such refund this posts the correcting entry (money.book_refund_fee_correction):

    debit  stripe_fees         fee
    credit platform_clearing   fee

Keyed `be1_fix:<order_id>`, so running it twice changes nothing the second time. Refunds
booked after the fix carry no stripe_fees line and are skipped.

Usage:
    python scripts/fix_be1_refund_ledger.py            # list what would be corrected
    python scripts/fix_be1_refund_ledger.py --apply    # post the corrections
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env", override=False)
env_name = os.getenv("FLASK_ENV", "development")
env_file = BASE_DIR / f".env.{env_name}"
if env_file.exists():
    load_dotenv(env_file, override=(env_name != "production"))

from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.ledger import ledger_service
from src.shared.models.ledger import (
    ACCOUNT_STRIPE_FEES,
    CREDIT,
    TXN_REFUND_ISSUED,
    LedgerAccount,
    LedgerEntry,
    LedgerTransaction,
)
from src.modules.payments import money


def affected(db) -> list[tuple]:
    """(order_id, fee) for every refund that credited the fee back."""
    fee_accounts = select(LedgerAccount.id).where(
        LedgerAccount.type == ACCOUNT_STRIPE_FEES, LedgerAccount.owner_id.is_(None)
    )
    rows = db.execute(
        select(LedgerTransaction.order_id, LedgerEntry.amount_cents)
        .join(LedgerEntry, LedgerEntry.transaction_id == LedgerTransaction.id)
        .where(
            LedgerTransaction.type == TXN_REFUND_ISSUED,
            LedgerEntry.direction == CREDIT,
            LedgerEntry.account_id.in_(fee_accounts),
            LedgerEntry.amount_cents > 0,
        )
        .order_by(LedgerTransaction.created_at)
    ).all()
    return [(order_id, fee) for order_id, fee in rows]


def main(apply: bool) -> int:
    db = SessionLocal()
    try:
        todo = [
            (order_id, fee) for order_id, fee in affected(db)
            if not ledger_service.get_existing_transaction(db, f"be1_fix:{order_id}")
        ]
        total = sum(fee for _, fee in todo)
        print(f"{len(todo)} refund(s) to correct, {total} cents in total.")
        for order_id, fee in todo:
            print(f"  order {order_id}: fee {fee} cents")
        if not apply:
            print("Dry run — nothing written. Re-run with --apply to post the corrections.")
            return 0
        for order_id, fee in todo:
            money.book_refund_fee_correction(db, order_id, fee, commit=False)
        db.commit()
        print(f"Posted {len(todo)} correction(s).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv[1:]))
