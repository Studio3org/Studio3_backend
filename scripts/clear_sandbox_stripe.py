"""One-off: clear sandbox Stripe Connect linkage now that the server runs live keys.

stripe_account_id / stripe_customer_id values minted against test-mode keys are meaningless
(and will error) against live keys — every artist who did payout setup, and every buyer who
saved a card, has to redo it once. This just clears the pointer so the app treats them as
never having started, rather than leaving stale sandbox ids that fail confusingly.

Usage (on the server, from /opt/studio3, with the venv active):
    FLASK_ENV=production python scripts/clear_sandbox_stripe.py            # dry run, lists rows
    FLASK_ENV=production python scripts/clear_sandbox_stripe.py --apply   # actually clears them
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

from src.shared.config.database import SessionLocal
from src.shared.models.user import User


def main():
    apply = "--apply" in sys.argv
    db = SessionLocal()
    try:
        rows = (
            db.query(User)
            .filter((User.stripe_account_id.isnot(None)) | (User.stripe_customer_id.isnot(None)))
            .all()
        )
        if not rows:
            print("Nothing to clear.")
            return
        for r in rows:
            print(
                f"{r.username} | account: {r.stripe_account_id} | "
                f"customer: {r.stripe_customer_id} | payouts_enabled: {r.stripe_payouts_enabled}"
            )
        print(f"total: {len(rows)}")

        if not apply:
            print("\nDry run only — rerun with --apply to actually clear these.")
            return

        for r in rows:
            r.stripe_account_id = None
            r.stripe_customer_id = None
            r.stripe_payouts_enabled = False
        db.commit()
        print(f"\nCleared {len(rows)} row(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
