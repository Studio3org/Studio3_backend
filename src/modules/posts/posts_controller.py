"""Posts controller."""
import uuid
from typing import Optional

from flask import request, g

from src.shared.config.database import SessionLocal
from src.shared.storage.s3_service import validate_user_media_url
from src.shared.utils.app_error import AppError
from src.modules.auth.auth_dao import find_user_by_username
from src.modules.user.user_dao import get_user_by_id
from src.modules.pieces.pieces_dao import get_piece, piece_to_dict
from src.modules.posts.posts_dao import (
    create_post,
    delete_post,
    get_post,
    list_user_posts,
    list_related_posts,
    list_saved_posts,
    post_to_dict,
)
from src.modules.social import social_dao


def create():
    body = request.get_json() or {}
    if body.get("isForSale") or body.get("priceCents"):
        raise AppError("Posts cannot be listed for sale.", 400)
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        media_url = body.get("mediaUrl")
        if not media_url:
            raise AppError("mediaUrl is required.", 400)
        validate_user_media_url(user.username, media_url)
        media_type = (body.get("mediaType") or "image").strip().lower()
        if media_type not in ("image", "video"):
            raise AppError("mediaType must be image or video.", 400)
        linked = body.get("linkedPieceId")
        linked_uuid = None
        if linked:
            piece = get_piece(db, uuid.UUID(linked))
            if not piece or piece.user_id != user.id:
                raise AppError("Linked piece not found.", 404)
            linked_uuid = piece.id
        is_process = body["isProcess"] if "isProcess" in body else False
        post = create_post(
            db,
            user_id=user.id,
            media_url=media_url,
            media_type=media_type,
            caption=body.get("caption"),
            location=body.get("location"),
            is_process=bool(is_process),
            linked_piece_id=linked_uuid,
            status="live",
        )
        return post_to_dict(post), 201
    finally:
        db.close()


def enrich_post_dict(db, post, viewer_id: Optional[uuid.UUID]) -> dict:
    base = post_to_dict(post)
    author = get_user_by_id(db, post.user_id)
    base["author"] = {
        "username": author.username,
        "name": author.name,
        "profilePhotoUrl": author.image,
        "isFollowing": social_dao.user_follows(db, viewer_id, author.id) if viewer_id else False,
    }
    base["likeCount"] = social_dao.count_likes(db, "post", post.id)
    base["commentCount"] = social_dao.count_comments(db, "post", post.id)
    base["isLiked"] = social_dao.user_liked(db, "post", post.id, viewer_id) if viewer_id else False
    base["isSaved"] = social_dao.user_saved(db, "post", post.id, viewer_id) if viewer_id else False
    if post.linked_piece_id:
        piece = get_piece(db, post.linked_piece_id)
        base["piece"] = piece_to_dict(piece) if piece else None
    else:
        base["piece"] = None
    return base


def get_detail(post_id: str, viewer_id: Optional[uuid.UUID] = None):
    db = SessionLocal()
    try:
        post = get_post(db, uuid.UUID(post_id))
        if not post:
            raise AppError("Post not found.", 404)
        return enrich_post_dict(db, post, viewer_id), 200
    finally:
        db.close()


def patch(post_id: str):
    body = request.get_json() or {}
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        post = get_post(db, uuid.UUID(post_id))
        if not post or post.user_id != user.id:
            raise AppError("Post not found.", 404)
        if "caption" in body:
            post.caption = body["caption"]
        if "location" in body:
            post.location = body["location"]
        if "linkedPieceId" in body:
            if body["linkedPieceId"]:
                piece = get_piece(db, uuid.UUID(body["linkedPieceId"]))
                if not piece or piece.user_id != user.id:
                    raise AppError("Linked piece not found.", 404)
                post.linked_piece_id = piece.id
            else:
                post.linked_piece_id = None
        db.commit()
        db.refresh(post)
        return post_to_dict(post), 200
    finally:
        db.close()


def delete(post_id: str):
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        post = get_post(db, uuid.UUID(post_id))
        if not post or post.user_id != user.id:
            raise AppError("Post not found.", 404)
        delete_post(db, post)
        return {"deleted": True}, 200
    finally:
        db.close()


def list_for_user(username: str):
    db = SessionLocal()
    try:
        user = find_user_by_username(db, username.lower())
        if not user:
            raise AppError("User not found.", 404)
        viewer_id = uuid.UUID(g.user["id"]) if getattr(g, "user", None) else None
        if not social_dao.can_view_content(db, user, viewer_id):
            raise AppError("This account is private.", 403)
        posts = list_user_posts(db, user.id)
        return [post_to_dict(p) for p in posts], 200
    finally:
        db.close()


def list_saved_for_me(user_id: uuid.UUID):
    db = SessionLocal()
    try:
        posts = list_saved_posts(db, user_id)
        return [enrich_post_dict(db, p, user_id) for p in posts], 200
    finally:
        db.close()


def related_for_piece(piece_id: str):
    db = SessionLocal()
    try:
        posts = list_related_posts(db, uuid.UUID(piece_id))
        return [post_to_dict(p) for p in posts], 200
    finally:
        db.close()
