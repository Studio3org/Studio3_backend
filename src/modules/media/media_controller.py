"""Media upload presign."""
import uuid

from flask import request, g

from src.shared.config.database import SessionLocal
from src.shared.storage.s3_service import presign_put
from src.shared.utils.app_error import AppError
from src.modules.user.user_dao import get_user_by_id


def presign():
    body = request.get_json() or {}
    purpose = (body.get("purpose") or "").strip().lower()
    content_type = (body.get("contentType") or body.get("content_type") or "").strip()
    content_id = (
        body.get("pieceId") or body.get("postId") or body.get("eventId")
        or body.get("content_id")
    )

    if purpose not in ("profile", "cover", "piece", "post", "chat", "event"):
        raise AppError("Invalid purpose.", 400)
    if not content_type:
        raise AppError("contentType is required.", 400)

    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        if not user:
            raise AppError("User not found.", 404)
        result = presign_put(user.username, purpose, content_type, content_id)
        return result, 200
    finally:
        db.close()
