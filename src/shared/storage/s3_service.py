"""S3 presign, validation, prefix migration."""

from __future__ import annotations


from flask import request
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.models.piece import Piece
from src.shared.models.piece_media import PieceMedia
from src.shared.models.post import Post
from src.shared.models.user import User
from src.shared.storage.s3_client import get_public_base_url, get_s3_client, get_bucket, s3_configured
from src.shared.storage.s3_paths import build_media_key, public_url
from src.shared.utils.app_error import AppError

IMAGE_MAX_BYTES = 20 * 1024 * 1024
VIDEO_MAX_BYTES = 100 * 1024 * 1024
ALLOWED_IMAGE = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_VIDEO = {"video/mp4"}


def _validate_content_type(content_type: str, purpose: str) -> None:
    ct = content_type.split(";")[0].strip().lower()
    if ct in ALLOWED_IMAGE or ct in ALLOWED_VIDEO:
        return
    raise AppError(f"Unsupported content type: {content_type}", 400)


def _effective_base_url() -> str:
    """Public base URL for media links; falls back to this server's own
    local-disk media endpoint when S3 isn't configured (dev)."""
    base = get_public_base_url()
    if base:
        return base
    return f"{request.host_url.rstrip('/')}/api/media/local"


def presign_put(username: str, purpose: str, content_type: str, content_id: str | None = None) -> dict:
    _validate_content_type(content_type, purpose)
    key = build_media_key(username, purpose, content_type, content_id)
    url = public_url(_effective_base_url(), key)

    if not s3_configured():
        # Partial S3 env (e.g. bucket set on Render but keys missing) used to
        # fall through to boto3 and blow up as a generic 500. Fail clearly.
        if get_bucket():
            raise AppError(
                "Media storage is misconfigured on the server "
                "(S3_BUCKET is set but AWS credentials are missing).",
                503,
            )
        # Dev fallback — PUT the bytes to this same server's local media store.
        return {
            "presignedPutUrl": url,
            "url": url,
            "key": key,
            "devMode": True,
        }

    try:
        client = get_s3_client()
        presigned = client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": get_bucket(),
                "Key": key,
                "ContentType": content_type.split(";")[0].strip(),
            },
            ExpiresIn=3600,
        )
    except Exception as exc:
        raise AppError(
            f"Could not create upload URL: {exc}",
            503,
        ) from exc
    return {"presignedPutUrl": presigned, "url": url, "key": key, "devMode": False}


def validate_user_media_url(username: str, media_url: str) -> None:
    base = _effective_base_url().rstrip("/")
    prefix = f"{base}/{username.lower()}/"
    if not media_url.startswith(prefix):
        raise AppError("Media URL must belong to your account.", 400)


def migrate_user_prefix(db: Session, user_id, old_username: str, new_username: str) -> None:
    if not s3_configured():
        _rewrite_urls_db(db, user_id, old_username, new_username)
        return

    client = get_s3_client()
    bucket = get_bucket()
    old_prefix = f"{old_username.lower()}/"
    new_prefix = f"{new_username.lower()}/"

    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=old_prefix):
        for obj in page.get("Contents", []):
            old_key = obj["Key"]
            new_key = new_prefix + old_key[len(old_prefix):]
            client.copy_object(
                Bucket=bucket,
                CopySource={"Bucket": bucket, "Key": old_key},
                Key=new_key,
            )
            client.delete_object(Bucket=bucket, Key=old_key)

    _rewrite_urls_db(db, user_id, old_username, new_username)


def _rewrite_urls_db(db: Session, user_id, old_username: str, new_username: str) -> None:
    old_prefix = f"{old_username.lower()}/"
    new_prefix = f"{new_username.lower()}/"
    base = _effective_base_url().rstrip("/")

    user = db.get(User, user_id)
    if user:
        if user.image and old_username.lower() in user.image.lower():
            user.image = user.image.replace(f"{base}/{old_prefix}", f"{base}/{new_prefix}").replace(old_prefix, new_prefix)
        if user.cover_photo_url and old_username.lower() in user.cover_photo_url.lower():
            user.cover_photo_url = user.cover_photo_url.replace(f"{base}/{old_prefix}", f"{base}/{new_prefix}").replace(old_prefix, new_prefix)

    piece_ids = []
    for piece in db.execute(select(Piece).where(Piece.user_id == user_id)).scalars():
        piece_ids.append(piece.id)
        if piece.media_url:
            piece.media_url = piece.media_url.replace(f"{base}/{old_prefix}", f"{base}/{new_prefix}")

    if piece_ids:
        for media in db.execute(
            select(PieceMedia).where(PieceMedia.piece_id.in_(piece_ids))
        ).scalars():
            if media.media_url:
                media.media_url = media.media_url.replace(f"{base}/{old_prefix}", f"{base}/{new_prefix}")

    for post in db.execute(select(Post).where(Post.user_id == user_id)).scalars():
        if post.media_url:
            post.media_url = post.media_url.replace(f"{base}/{old_prefix}", f"{base}/{new_prefix}")

    db.commit()
