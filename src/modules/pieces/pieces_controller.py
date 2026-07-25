"""Pieces controller."""
import uuid
from typing import Optional

from flask import request, g
from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.models.user import User
from src.shared.storage.s3_service import validate_user_media_url
from src.shared.utils.app_error import AppError
from src.modules.auth.auth_dao import find_user_by_username
from src.modules.user.user_dao import get_user_by_id
from src.modules.pieces.pieces_dao import (
    create_piece,
    delete_piece,
    get_piece,
    list_user_pieces,
    list_saved_pieces,
    piece_to_dict,
)
from src.modules.social import social_dao
from src.modules.series import series_dao


def _validate_sale_fields(body, seller_enabled: bool):
    if not body.get("isForSale"):
        return
    if not seller_enabled:
        raise AppError("Enable seller mode before listing for sale.", 403)
    price = body.get("priceCents")
    if not price or int(price) < 100:
        raise AppError("Price must be at least $1.00 (100 cents).", 400)
    if not body.get("medium"):
        raise AppError("Medium is required for sale listings.", 400)
    if not body.get("dimensions"):
        raise AppError("Dimensions are required for sale listings.", 400)
    if not body.get("shippingRegion"):
        raise AppError("Shipping region is required for sale listings.", 400)


def create():
    body = request.get_json() or {}
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        media_url = body.get("mediaUrl")
        if not media_url:
            raise AppError("mediaUrl is required.", 400)
        validate_user_media_url(user.username, media_url)
        _validate_sale_fields(body, user.seller_enabled)
        piece = create_piece(
            db,
            user_id=user.id,
            title=(body.get("title") or "Untitled")[:200],
            media_url=media_url,
            media_type=body.get("mediaType", "image"),
            caption=body.get("caption"),
            medium=body.get("medium"),
            materials=body.get("materials"),
            style_tags=body.get("styleTags"),
            ai_disclosed=bool(body.get("aiDisclosed", False)),
            alt_text=body.get("altText"),
            is_for_sale=bool(body.get("isForSale", False)),
            price_cents=body.get("priceCents"),
            currency=body.get("currency", "USD"),
            dimensions=body.get("dimensions"),
            shipping_region=body.get("shippingRegion"),
            location=body.get("location"),
            media_aspect_ratio=body.get("mediaAspectRatio"),
            year_created=body.get("yearCreated"),
            framing_mounting=body.get("framingMounting"),
            provenance=body.get("provenance"),
            handling_notes=body.get("handlingNotes"),
            status="draft" if body.get("status") == "draft" else "live",
        )
        return piece_to_dict(piece), 201
    finally:
        db.close()


def enrich_piece_dict(db, piece, viewer_id: Optional[uuid.UUID]) -> dict:
    from src.modules.posts.posts_dao import list_related_posts, post_to_dict

    base = piece_to_dict(piece)
    author = get_user_by_id(db, piece.user_id)
    base["author"] = {
        "username": author.username,
        "name": author.name,
        "profilePhotoUrl": author.image,
        "isFollowing": social_dao.user_follows(db, viewer_id, author.id) if viewer_id else False,
    }
    base["likeCount"] = social_dao.count_likes(db, "piece", piece.id)
    base["commentCount"] = social_dao.count_comments(db, "piece", piece.id)
    base["isLiked"] = social_dao.user_liked(db, "piece", piece.id, viewer_id) if viewer_id else False
    base["isSaved"] = social_dao.user_saved(db, "piece", piece.id, viewer_id) if viewer_id else False
    series = series_dao.get_series_for_piece(db, piece.id)
    base["series"] = series_dao.series_detail_dict(db, series) if series else None
    base["relatedPosts"] = [post_to_dict(p) for p in list_related_posts(db, piece.id)]
    return base


def get_detail(piece_id: str, viewer_id: Optional[uuid.UUID] = None):
    db = SessionLocal()
    try:
        piece = get_piece(db, uuid.UUID(piece_id))
        if not piece:
            raise AppError("Piece not found.", 404)
        return enrich_piece_dict(db, piece, viewer_id), 200
    finally:
        db.close()


def patch(piece_id: str):
    body = request.get_json() or {}
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        piece = get_piece(db, uuid.UUID(piece_id))
        if not piece or piece.user_id != user.id:
            raise AppError("Piece not found.", 404)
        if "mediaUrl" in body and body["mediaUrl"]:
            validate_user_media_url(user.username, body["mediaUrl"])
            piece.media_url = body["mediaUrl"]
        for attr, key in [
            ("title", "title"), ("caption", "caption"), ("medium", "medium"),
            ("alt_text", "altText"), ("shipping_region", "shippingRegion"),
            ("location", "location"),
            ("year_created", "yearCreated"), ("framing_mounting", "framingMounting"),
            ("provenance", "provenance"), ("handling_notes", "handlingNotes"),
        ]:
            if key in body:
                setattr(piece, attr, body[key])
        if "materials" in body:
            piece.materials = body["materials"]
        if "styleTags" in body:
            piece.style_tags = body["styleTags"]
        if "isForSale" in body:
            _validate_sale_fields({**piece_to_dict(piece), **body, "isForSale": body["isForSale"]}, user.seller_enabled)
            piece.is_for_sale = bool(body["isForSale"])
        if "priceCents" in body:
            piece.price_cents = body["priceCents"]
        if "dimensions" in body:
            piece.dimensions = body["dimensions"]
        if "status" in body:
            piece.status = body["status"]
        db.commit()
        db.refresh(piece)
        return piece_to_dict(piece), 200
    finally:
        db.close()


def delete(piece_id: str):
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        piece = get_piece(db, uuid.UUID(piece_id))
        if not piece or piece.user_id != user.id:
            raise AppError("Piece not found.", 404)
        delete_piece(db, piece)
        return {"deleted": True}, 200
    finally:
        db.close()


def list_for_user(username: str, for_sale_only: bool = False):
    db = SessionLocal()
    try:
        user = find_user_by_username(db, username.lower())
        if not user:
            raise AppError("User not found.", 404)
        viewer_id = uuid.UUID(g.user["id"]) if getattr(g, "user", None) else None
        if not social_dao.can_view_content(db, user, viewer_id):
            raise AppError("This account is private.", 403)
        pieces = list_user_pieces(
            db,
            user.id,
            for_sale_only=for_sale_only,
            include_drafts=viewer_id == user.id,
        )
        return [piece_to_dict(p) for p in pieces], 200
    finally:
        db.close()


def list_saved_for_me(user_id: uuid.UUID):
    """Lightweight saved-pieces list — batched engagement, no relatedPosts/series N+1."""
    db = SessionLocal()
    try:
        pieces = list_saved_pieces(db, user_id)
        if not pieces:
            return [], 200

        piece_ids = [p.id for p in pieces]
        like_counts = social_dao.batch_like_counts(db, "piece", piece_ids)
        liked_ids = social_dao.batch_user_likes(db, "piece", piece_ids, user_id)
        author_ids = {p.user_id for p in pieces}
        authors = {
            u.id: u
            for u in db.execute(select(User).where(User.id.in_(author_ids))).scalars()
        } if author_ids else {}

        result = []
        for piece in pieces:
            base = piece_to_dict(piece)
            author = authors.get(piece.user_id)
            base["author"] = {
                "username": author.username if author else None,
                "name": author.name if author else None,
                "profilePhotoUrl": author.image if author else None,
                "isFollowing": False,
            }
            base["likeCount"] = like_counts.get(piece.id, 0)
            base["commentCount"] = 0
            base["isLiked"] = piece.id in liked_ids
            base["isSaved"] = True
            base["series"] = None
            base["relatedPosts"] = []
            result.append(base)
        return result, 200
    finally:
        db.close()
