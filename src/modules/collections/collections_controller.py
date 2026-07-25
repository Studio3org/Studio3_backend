"""Collections controller."""
import uuid

from flask import request, g

from src.shared.config.database import SessionLocal
from src.shared.utils.app_error import AppError
from src.modules.user.user_dao import get_user_by_id
from src.modules.pieces.pieces_dao import get_piece
from src.modules.posts.posts_dao import get_post
from src.modules.collections import collections_dao

_VALID_TARGET_TYPES = ("piece", "post")


def _get_target(db, target_type: str, target_id: uuid.UUID):
    if target_type == "piece":
        return get_piece(db, target_id)
    if target_type == "post":
        return get_post(db, target_id)
    return None


def _cover_dict(db, item) -> dict | None:
    if item is None:
        return None
    target = _get_target(db, item.target_type, item.target_id)
    if not target:
        return None
    return {
        "targetType": item.target_type,
        "targetId": str(item.target_id),
        "mediaUrl": target.media_url,
    }


def _collection_summary_dict(db, collection) -> dict:
    return {
        "id": str(collection.id),
        "name": collection.name,
        "createdAt": collection.created_at.isoformat(),
        "itemCount": collections_dao.count_items(db, collection.id),
        "cover": _cover_dict(db, collections_dao.most_recent_item(db, collection.id)),
    }


def _require_owned_collection(db, collection_id: uuid.UUID, user_id: uuid.UUID):
    collection = collections_dao.get_collection(db, collection_id)
    if not collection or collection.user_id != user_id:
        raise AppError("Collection not found.", 404)
    return collection


def list_for_me():
    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        collections = collections_dao.list_user_collections(db, user_id)
        return [_collection_summary_dict(db, c) for c in collections], 200
    finally:
        db.close()


def create():
    body = request.get_json() or {}
    name = (body.get("name") or "").strip()
    if not name:
        raise AppError("Collection name is required.", 400)
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        collection = collections_dao.create_collection(db, user.id, name[:120])
        return _collection_summary_dict(db, collection), 201
    finally:
        db.close()


def rename(collection_id: str):
    body = request.get_json() or {}
    name = (body.get("name") or "").strip()
    if not name:
        raise AppError("Collection name is required.", 400)
    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        collection = _require_owned_collection(db, uuid.UUID(collection_id), user_id)
        collections_dao.rename_collection(db, collection, name[:120])
        return _collection_summary_dict(db, collection), 200
    finally:
        db.close()


def delete(collection_id: str):
    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        collection = _require_owned_collection(db, uuid.UUID(collection_id), user_id)
        collections_dao.delete_collection(db, collection)
        return {"deleted": True}, 200
    finally:
        db.close()


def get_detail(collection_id: str):
    """Collection detail with batched engagement — avoids per-item enrich_* N+1."""
    from sqlalchemy import select

    from src.shared.models.user import User
    from src.modules.pieces.pieces_dao import piece_to_dict
    from src.modules.posts.posts_dao import post_to_dict
    from src.modules.social import social_dao

    db = SessionLocal()
    try:
        viewer_id = uuid.UUID(g.user["id"])
        collection = _require_owned_collection(db, uuid.UUID(collection_id), viewer_id)
        items = collections_dao.list_items(db, collection.id)

        piece_ids = [i.target_id for i in items if i.target_type == "piece"]
        post_ids = [i.target_id for i in items if i.target_type == "post"]
        pieces_by_id = {}
        posts_by_id = {}
        if piece_ids:
            from src.shared.models.piece import Piece
            pieces_by_id = {
                p.id: p
                for p in db.execute(
                    select(Piece).where(Piece.id.in_(piece_ids), Piece.deleted_at.is_(None))
                ).scalars()
            }
        if post_ids:
            from src.shared.models.post import Post
            posts_by_id = {
                p.id: p
                for p in db.execute(
                    select(Post).where(Post.id.in_(post_ids), Post.deleted_at.is_(None))
                ).scalars()
            }

        piece_like_counts = social_dao.batch_like_counts(db, "piece", list(pieces_by_id.keys()))
        post_like_counts = social_dao.batch_like_counts(db, "post", list(posts_by_id.keys()))
        piece_comment_counts = social_dao.batch_comment_counts(db, "piece", list(pieces_by_id.keys()))
        post_comment_counts = social_dao.batch_comment_counts(db, "post", list(posts_by_id.keys()))
        liked_pieces = social_dao.batch_user_likes(db, "piece", list(pieces_by_id.keys()), viewer_id)
        liked_posts = social_dao.batch_user_likes(db, "post", list(posts_by_id.keys()), viewer_id)
        saved_pieces = social_dao.batch_user_saves(db, "piece", list(pieces_by_id.keys()), viewer_id)
        saved_posts = social_dao.batch_user_saves(db, "post", list(posts_by_id.keys()), viewer_id)

        author_ids = {p.user_id for p in pieces_by_id.values()} | {p.user_id for p in posts_by_id.values()}
        authors = {
            u.id: u for u in db.execute(select(User).where(User.id.in_(author_ids))).scalars()
        } if author_ids else {}
        following_authors = social_dao.batch_accepted_following(db, viewer_id, list(author_ids))

        def _author(user_id):
            author = authors.get(user_id)
            if not author:
                return None
            return {
                "username": author.username,
                "name": author.name,
                "profilePhotoUrl": author.image,
                "isFollowing": user_id in following_authors,
            }

        enriched = []
        for item in items:
            if item.target_type == "piece":
                target = pieces_by_id.get(item.target_id)
                if not target:
                    continue
                d = piece_to_dict(target)
                d["author"] = _author(target.user_id)
                d["likeCount"] = piece_like_counts.get(target.id, 0)
                d["commentCount"] = piece_comment_counts.get(target.id, 0)
                d["isLiked"] = target.id in liked_pieces
                d["isSaved"] = target.id in saved_pieces
                d["series"] = None
                d["relatedPosts"] = []
            else:
                target = posts_by_id.get(item.target_id)
                if not target:
                    continue
                d = post_to_dict(target)
                d["author"] = _author(target.user_id)
                d["likeCount"] = post_like_counts.get(target.id, 0)
                d["commentCount"] = post_comment_counts.get(target.id, 0)
                d["isLiked"] = target.id in liked_posts
                d["isSaved"] = target.id in saved_posts
                d["piece"] = None
            d["targetType"] = item.target_type
            enriched.append(d)

        return {
            "id": str(collection.id),
            "name": collection.name,
            "createdAt": collection.created_at.isoformat(),
            "itemCount": len(enriched),
            "items": enriched,
        }, 200
    finally:
        db.close()


def add_item(collection_id: str):
    body = request.get_json() or {}
    target_type = body.get("targetType")
    target_id = body.get("targetId")
    if target_type not in _VALID_TARGET_TYPES:
        raise AppError("targetType must be 'piece' or 'post'.", 400)
    if not target_id:
        raise AppError("targetId is required.", 400)
    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        collection = _require_owned_collection(db, uuid.UUID(collection_id), user_id)
        tid = uuid.UUID(target_id)
        if not _get_target(db, target_type, tid):
            raise AppError(f"{target_type.capitalize()} not found.", 404)
        collections_dao.add_item(db, collection.id, target_type, tid)
        return {"added": True, "itemCount": collections_dao.count_items(db, collection.id)}, 200
    finally:
        db.close()


def remove_item(collection_id: str, target_type: str, target_id: str):
    if target_type not in _VALID_TARGET_TYPES:
        raise AppError("targetType must be 'piece' or 'post'.", 400)
    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        collection = _require_owned_collection(db, uuid.UUID(collection_id), user_id)
        collections_dao.remove_item(db, collection.id, target_type, uuid.UUID(target_id))
        return {"removed": True, "itemCount": collections_dao.count_items(db, collection.id)}, 200
    finally:
        db.close()
