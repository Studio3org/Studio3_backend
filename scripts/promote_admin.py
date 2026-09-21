"""Grant or revoke admin access for an existing user.

There is deliberately no self-serve admin signup — admin accounts can issue refunds and
release artist payouts, so the only way in is someone with database access running this.

Usage:
    venv/Scripts/python.exe scripts/promote_admin.py user@example.com
    venv/Scripts/python.exe scripts/promote_admin.py user@example.com --revoke
    venv/Scripts/python.exe scripts/promote_admin.py --list
"""
import argparse
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
from src.shared.models.user import User


def list_admins(db) -> int:
    admins = list(db.execute(select(User).where(User.is_admin.is_(True))).scalars().all())
    if not admins:
        print("No admin users.")
        return 0
    print(f"{len(admins)} admin user(s):")
    for user in admins:
        print(f"  {user.email:<40} {user.name}")
    return 0


def set_admin(db, email: str, grant: bool) -> int:
    user = db.execute(select(User).where(User.email == email.lower())).scalar_one_or_none()
    if not user:
        print(f"No user with email {email!r}.", file=sys.stderr)
        return 1

    if user.is_admin == grant:
        print(f"{user.email} is already {'an admin' if grant else 'not an admin'}. Nothing to do.")
        return 0

    if not grant:
        remaining = db.execute(
            select(User).where(User.is_admin.is_(True), User.id != user.id)
        ).scalars().first()
        if not remaining:
            print(
                f"Refusing to revoke {user.email}: they are the only admin, and revoking "
                "would leave nobody able to resolve disputes or release payouts.",
                file=sys.stderr,
            )
            return 1

    if grant and not user.password:
        # The admin UI is email + password only; an OAuth-only account could never sign in.
        print(
            f"Refusing to promote {user.email}: the account has no password set, so it "
            "could not sign in to the admin UI.",
            file=sys.stderr,
        )
        return 1

    user.is_admin = grant
    db.commit()
    print(f"{'Granted' if grant else 'Revoked'} admin for {user.email}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", nargs="?", help="Email of the user to grant/revoke.")
    parser.add_argument("--revoke", action="store_true", help="Revoke instead of grant.")
    parser.add_argument("--list", action="store_true", help="List current admins and exit.")
    args = parser.parse_args()

    if not args.list and not args.email:
        parser.error("provide an email, or --list")

    db = SessionLocal()
    try:
        if args.list:
            return list_admins(db)
        return set_admin(db, args.email, grant=not args.revoke)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
