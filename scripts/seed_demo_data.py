"""Seed (or remove) launch-event demo accounts + pieces/scenes/series.

Reads scripts/demo_data.json and inserts real rows via SQLAlchemy directly
into the same database the app itself uses (there is no separate local DB —
see database.py / .env.development). Every demo user's email is tagged with
a distinct, obviously-fake domain (see "emailDomain" in the JSON) so this
data is always identifiable and safe to remove later.

Usage:
    venv/Scripts/python.exe scripts/seed_demo_data.py          # seed
    venv/Scripts/python.exe scripts/seed_demo_data.py --undo   # remove all demo data
"""
import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import bcrypt

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env", override=False)
env_name = os.getenv("FLASK_ENV", "development")
env_file = BASE_DIR / f".env.{env_name}"
if env_file.exists():
    load_dotenv(env_file, override=(env_name != "production"))

from sqlalchemy import or_

from src.shared.config.database import SessionLocal
from src.shared.models.piece import Piece
from src.shared.models.post import Post
from src.shared.models.series import Series, SeriesPiece
from src.shared.models.user import User

SALT_ROUNDS = int(os.getenv("SALT_ROUNDS", "10"))
DATA_FILE = Path(__file__).resolve().parent / "demo_data.json"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=SALT_ROUNDS)).decode()


def seed(data: dict) -> None:
    email_domain = data["emailDomain"]
    password_hash = hash_password(data["demoPassword"])
    db = SessionLocal()
    try:
        for account in data["accounts"]:
            username = account["username"]
            email = f"{username}@{email_domain}"
            existing = (
                db.query(User)
                .filter(or_(User.username == username, User.email == email))
                .first()
            )
            if existing:
                print(f"  {username}: already exists, skipping")
                continue

            user = User(
                id=uuid.uuid4(),
                username=username,
                email=email,
                name=account["name"],
                password=password_hash,
                image=account["avatarUrl"],
                bio=account["bio"],
                location=account["location"],
                email_verified=True,
                seller_enabled=True,
                onboarding_complete=True,
            )
            db.add(user)
            db.flush()

            piece_id_by_key = {}
            all_pieces = list(account["series"]["pieces"]) + list(account["standalonePieces"])
            for piece_data in all_pieces:
                piece = Piece(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    title=piece_data["title"],
                    media_url=piece_data["mediaUrl"],
                    media_type="image",
                    caption=piece_data["caption"],
                    materials=account["materials"],
                    style_tags=account["styleTags"],
                    is_for_sale=piece_data["isForSale"],
                    price_cents=piece_data.get("priceCents"),
                    location=account["location"],
                    media_aspect_ratio=piece_data["mediaAspectRatio"],
                )
                db.add(piece)
                db.flush()
                piece_id_by_key[piece_data["key"]] = piece.id

            series = Series(id=uuid.uuid4(), user_id=user.id, name=account["series"]["name"])
            db.add(series)
            db.flush()
            for position, piece_data in enumerate(account["series"]["pieces"]):
                db.add(
                    SeriesPiece(
                        id=uuid.uuid4(),
                        series_id=series.id,
                        piece_id=piece_id_by_key[piece_data["key"]],
                        position=position,
                    )
                )

            for scene in account["scenes"]:
                linked_key = scene.get("linkedPieceKey")
                db.add(
                    Post(
                        id=uuid.uuid4(),
                        user_id=user.id,
                        media_url=scene["mediaUrl"],
                        media_type=scene["mediaType"],
                        caption=scene["caption"],
                        location=account["location"],
                        media_aspect_ratio=scene["mediaAspectRatio"],
                        thumbnail_url=scene.get("thumbnailUrl"),
                        linked_piece_id=piece_id_by_key.get(linked_key) if linked_key else None,
                    )
                )

            db.commit()
            print(
                f"  {username}: created ({len(all_pieces)} pieces, "
                f"1 series, {len(account['scenes'])} scenes)"
            )

        total_users = (
            db.query(User).filter(User.email.like(f"%@{email_domain}")).count()
        )
        total_pieces = (
            db.query(Piece).join(User, Piece.user_id == User.id)
            .filter(User.email.like(f"%@{email_domain}"))
            .count()
        )
        total_posts = (
            db.query(Post).join(User, Post.user_id == User.id)
            .filter(User.email.like(f"%@{email_domain}"))
            .count()
        )
        print(f"\nTotals for @{email_domain}: {total_users} users, {total_pieces} pieces, {total_posts} scenes")
        print(f"Shared demo password: {data['demoPassword']}")
        print("Usernames: " + ", ".join(a["username"] for a in data["accounts"]))
    finally:
        db.close()


def undo(data: dict) -> None:
    email_domain = data["emailDomain"]
    db = SessionLocal()
    try:
        deleted = (
            db.query(User)
            .filter(User.email.like(f"%@{email_domain}"))
            .delete(synchronize_session=False)
        )
        db.commit()
        print(f"Deleted {deleted} demo user(s) (pieces/posts/series cascade automatically).")
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--undo", action="store_true", help="remove all seeded demo data")
    args = parser.parse_args()

    with open(DATA_FILE, encoding="utf-8") as f:
        data = json.load(f)

    if args.undo:
        undo(data)
    else:
        seed(data)


if __name__ == "__main__":
    main()
