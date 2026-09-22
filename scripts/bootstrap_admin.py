"""Create the first admin from the environment, so a fresh deployment has a way in.

There is no self-serve admin signup — an admin can issue refunds and release artist
payouts, so the only way to become one is someone with database access saying so. That
leaves a new environment with nobody able to sign in to the console at all, which is what
this solves and the only thing it solves.

Reads ADMIN_EMAIL and ADMIN_PASSWORD. With either unset it does nothing and exits 0, so it
is safe to run unconditionally from a deploy.

Deliberately create-only. If the account already exists it is promoted to admin and the
**password is left alone**, because this runs on every deploy: resetting the password each
time would silently undo any rotation, and an env file is a worse place to keep a live
credential than the operator's own password manager.

Usage:
    python scripts/bootstrap_admin.py
"""
import os
import re
import sys
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env", override=False)
env_name = os.getenv("FLASK_ENV", "development")
env_file = BASE_DIR / f".env.{env_name}"
if env_file.exists():
    load_dotenv(env_file, override=(env_name != "production"))

import bcrypt
from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.models.user import User

MIN_PASSWORD_LEN = 8
SALT_ROUNDS = int(os.getenv("SALT_ROUNDS", "10"))


def _username_from(email: str, db) -> str:
    """A valid, unused username derived from the email's local part.

    The column is unique and not null, so something has to be chosen; the local part is the
    least surprising thing to find in the console later.
    """
    base = re.sub(r"[^a-z0-9_]", "", email.split("@")[0].lower())[:24] or "admin"
    candidate = base
    suffix = 1
    while db.execute(select(User).where(User.username == candidate)).scalar_one_or_none():
        suffix += 1
        candidate = f"{base}{suffix}"[:30]
    return candidate


def main() -> int:
    email = (os.getenv("ADMIN_EMAIL") or "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD") or ""

    if not email or not password:
        print("ADMIN_EMAIL/ADMIN_PASSWORD not set — skipping admin bootstrap.")
        return 0

    if "@" not in email:
        print(f"ADMIN_EMAIL {email!r} is not an email address.", file=sys.stderr)
        return 1
    if len(password) < MIN_PASSWORD_LEN:
        # Same rule as signup. This account can move other people's money, so it is not the
        # place to make an exception for a short password.
        print(f"ADMIN_PASSWORD must be at least {MIN_PASSWORD_LEN} characters.", file=sys.stderr)
        return 1

    db = SessionLocal()
    try:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()

        if user:
            if user.is_admin:
                print(f"{email} is already an admin. Nothing to do.")
                return 0
            user.is_admin = True
            db.commit()
            print(f"Promoted the existing account {email} to admin.")
            print("Its password was left unchanged — sign in with the one it already had.")
            return 0

        username = _username_from(email, db)
        db.add(User(
            id=uuid.uuid4(),
            email=email,
            username=username,
            name="Studio 3 Admin",
            password=bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=SALT_ROUNDS)).decode(),
            is_admin=True,
            # Ops-only account: it exists to reach the console, not to browse or sell. Marked
            # onboarded so it is never sent through the member onboarding flow.
            onboarding_complete=True,
            email_verified=True,
            role="collector",
        ))
        db.commit()
        print(f"Created admin {email} (@{username}).")
        print("Change this password after the first sign-in, then clear ADMIN_PASSWORD from")
        print("the environment — a live credential does not belong in a deploy config.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
