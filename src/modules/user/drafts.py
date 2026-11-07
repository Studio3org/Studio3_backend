"""Drafts: every piece and Scene someone started but didn't post (BACKEND_CHANGES B33).

`GET /api/user/me/drafts` lists both kinds, newest edit first. Each draft carries
`updatedAt`, `draftStep` (the posting step it stopped on, as the app names it) and
`publishFailed` (it was saved, then going live failed), so every device shows "Didn't
publish. Tap to try again." Going live clears both.
"""
import re
import uuid

from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.models.piece import Piece
from src.shared.models.post import Post
from src.shared.utils.app_error import AppError

# The app's own step names ("photos", "details", "price"...): short, lower case.
_STEP = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def step_from(body: dict):
    """The `draftStep` a create body names, checked (None when it names none)."""
    step = body.get("draftStep")
    if step is not None and (not isinstance(step, str) or not _STEP.match(step)):
        raise AppError("draftStep must be a short step name.", 400)
    return step


def apply_fields(row, body: dict) -> None:
    """`draftStep` and `publishFailed` from a create or PATCH body, onto a piece or a Scene."""
    if "draftStep" in body:
        row.draft_step = step_from(body)
    if "publishFailed" in body:
        failed = body.get("publishFailed")
        if not isinstance(failed, bool):
            raise AppError("publishFailed must be true or false.", 400)
        row.publish_failed = failed


def clear_when_live(row) -> None:
    """Posted: no step left and nothing failed."""
    if row.status == "live":
        row.draft_step = None
        row.publish_failed = False


def list_for_me(user_id: uuid.UUID):
    from src.modules.pieces.pieces_dao import batch_list_piece_media, piece_to_dict
    from src.modules.posts.posts_dao import post_to_dict

    db = SessionLocal()
    try:
        pieces = list(
            db.execute(
                select(Piece).where(
                    Piece.user_id == user_id,
                    Piece.status == "draft",
                    Piece.deleted_at.is_(None),
                )
            ).scalars()
        )
        posts = list(
            db.execute(
                select(Post).where(
                    Post.user_id == user_id,
                    Post.status == "draft",
                    Post.deleted_at.is_(None),
                )
            ).scalars()
        )
        media = batch_list_piece_media(db, [p.id for p in pieces])
        rows = []
        for piece in pieces:
            d = piece_to_dict(piece, media=media.get(piece.id, []))
            d["type"] = "piece"
            rows.append((piece.updated_at or piece.created_at, piece.id, d))
        for post in posts:
            d = post_to_dict(post)
            d["type"] = "post"
            rows.append((post.updated_at or post.created_at, post.id, d))
        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
        return {"items": [d for _, _, d in rows]}, 200
    finally:
        db.close()
