"""Pieces controller."""
import uuid
from typing import Optional

from flask import request, g
from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.storage.s3_service import validate_user_media_url
from src.shared.utils.app_error import AppError
from src.modules.auth.auth_dao import find_user_by_username
from src.modules.user.user_dao import get_user_by_id
from src.modules.pieces.pieces_dao import (
    batch_list_piece_media,
    create_piece,
    delete_piece,
    get_piece,
    list_piece_media,
    list_user_pieces,
    list_saved_pieces,
    piece_to_dict,
    replace_piece_media,
)
from src.modules.social import social_dao
from src.modules.series import series_dao
from src.modules.bids import auction_dao, bid_dao
from src.modules.pieces import listing_rules, piece_state


# Packaged size + weight are what couriers actually price on, and the declared value is
# needed for customs/insurance. Collected up front so ops can book a courier without going
# back to the artist for measurements.
_SHIPPING_FIELDS = (
    ("weightKg", "Package weight"),
    ("packageLengthCm", "Package length"),
    ("packageWidthCm", "Package width"),
    ("packageHeightCm", "Package height"),
)


def _current_duration_days(db, current) -> int | None:
    """The duration of the piece's running auction, if it has one.

    Lives on the Auction now, so a patch that omits auctionDurationDays still validates
    against the real value rather than silently reading None and failing the range check.
    """
    if current is None:
        return None
    auction = auction_dao.get_running_auction(db, current.id)
    return auction.duration_days if auction else None


def _resolve_listing(db, body, current, user) -> dict:
    """Validate the listing shape, merged over whatever the piece already is.

    Merging matters: a lone {"priceCents": 5000} on an auction still has to be checked
    against the auction rules. Validating only the keys present in the body is how a live
    auction could be switched to fixed-price, orphaning every bid on it.
    """
    is_for_sale = bool(body["isForSale"]) if "isForSale" in body else (
        bool(current.is_for_sale) if current is not None else False
    )
    return listing_rules.validate_listing(
        is_for_sale=is_for_sale,
        listing_type=body.get(
            "listingType", current.listing_type if current is not None else None
        ),
        price_cents=body.get(
            "priceCents", current.price_cents if current is not None else None
        ),
        auction_duration_days=body.get(
            "auctionDurationDays", _current_duration_days(db, current)
        ),
        seller_enabled=user.seller_enabled,
        seller=user,
        reserve_cents=body.get("reserveCents"),
    )


def _validate_sale_detail_fields(body):
    """Presentation-tier requirements for a sale listing — medium, dimensions, packaging.

    Separate from listing_rules, which owns the invariants the database also enforces.
    """
    if not body.get("isForSale"):
        return
    if not body.get("medium"):
        raise AppError("Medium is required for sale listings.", 400)
    if not body.get("dimensions"):
        raise AppError("Dimensions are required for sale listings.", 400)
    # Packaged shipping attrs are optional on this posting flow (Figma 2720:6711
    # Details tab has artwork W×H only). When present they still have to be
    # positive numbers so ops aren't given unusable values.
    for key, label in _SHIPPING_FIELDS:
        value = body.get(key)
        if value is None:
            continue
        try:
            if float(value) <= 0:
                raise AppError(f"{label} must be greater than zero.", 400)
        except (TypeError, ValueError):
            raise AppError(f"{label} must be a number.", 400)
    declared = body.get("declaredValueCents")
    if declared is not None:
        try:
            if int(declared) <= 0:
                raise AppError("Declared value must be greater than zero.", 400)
        except (TypeError, ValueError):
            raise AppError("Declared value must be a number.", 400)


def _extract_images(body: dict) -> list[dict]:
    """Accepts the new ordered gallery shape (`images`, or the simpler
    `mediaUrls` list) or the legacy single `mediaUrl` field — always
    returns an ordered list where index 0 is the cover. Piece flow's
    "Set your cover" step (Figma 2716:5774) sends `images` in the order the
    user dragged them into; old clients still sending a bare `mediaUrl`
    keep working unchanged."""
    images = body.get("images")
    if images:
        return [
            {
                "mediaUrl": img["mediaUrl"],
                "mediaType": img.get("mediaType", "image"),
                "mediaAspectRatio": img.get("mediaAspectRatio") or body.get("mediaAspectRatio"),
            }
            for img in images
            if img.get("mediaUrl")
        ]
    media_urls = body.get("mediaUrls")
    if media_urls:
        return [
            {
                "mediaUrl": url,
                "mediaType": body.get("mediaType", "image"),
                "mediaAspectRatio": body.get("mediaAspectRatio"),
            }
            for url in media_urls
            if url
        ]
    media_url = body.get("mediaUrl")
    if media_url:
        return [
            {
                "mediaUrl": media_url,
                "mediaType": body.get("mediaType", "image"),
                "mediaAspectRatio": body.get("mediaAspectRatio"),
            }
        ]
    return []


def create():
    body = request.get_json() or {}
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        images = _extract_images(body)
        if not images:
            raise AppError("mediaUrl is required.", 400)
        for image in images:
            validate_user_media_url(user.username, image["mediaUrl"])
        listing = _resolve_listing(db, body, None, user)
        _validate_sale_detail_fields(body)
        cover = images[0]
        piece = create_piece(
            db,
            user_id=user.id,
            title=(body.get("title") or "Untitled")[:200],
            media_url=cover["mediaUrl"],
            media_type=cover.get("mediaType", "image"),
            caption=body.get("caption"),
            medium=body.get("medium"),
            materials=body.get("materials"),
            style_tags=body.get("styleTags"),
            ai_disclosed=bool(body.get("aiDisclosed", False)),
            alt_text=body.get("altText"),
            is_for_sale=listing["is_for_sale"],
            listing_type=listing["listing_type"],
            price_cents=listing["price_cents"],
            currency=body.get("currency", "USD"),
            dimensions=body.get("dimensions"),
            shipping_region=body.get("shippingRegion"),
            weight_kg=body.get("weightKg"),
            package_length_cm=body.get("packageLengthCm"),
            package_width_cm=body.get("packageWidthCm"),
            package_height_cm=body.get("packageHeightCm"),
            declared_value_cents=body.get("declaredValueCents"),
            location=body.get("location"),
            media_aspect_ratio=cover.get("mediaAspectRatio"),
            year_created=body.get("yearCreated"),
            framing_mounting=body.get("framingMounting"),
            provenance=body.get("provenance"),
            handling_notes=body.get("handlingNotes"),
            status="draft" if body.get("status") == "draft" else "live",
        )
        if listing["listing_type"] == "auction":
            # The auction row, not the piece, owns the run. Created in draft here and opened
            # when the piece goes live, so a piece that sits in draft for a week does not
            # publish an auction that is already a week old.
            auction = auction_dao.create_auction(
                db,
                piece,
                user,
                starting_bid_cents=listing["price_cents"],
                duration_days=listing["auction_duration_days"],
                reserve_cents=listing.get("reserve_cents"),
                commit=False,
            )
            if piece.status == piece_state.LIVE:
                auction_dao.open_auction(db, auction, commit=False)
        db.commit()
        db.refresh(piece)
        replace_piece_media(db, piece.id, images)
        return piece_to_dict(piece, media=list_piece_media(db, piece.id)), 201
    finally:
        db.close()


def enrich_piece_dict(db, piece, viewer_id: Optional[uuid.UUID]) -> dict:
    from src.modules.posts.posts_dao import list_related_posts, post_to_dict

    base = piece_to_dict(piece, media=list_piece_media(db, piece.id))
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
    if piece.listing_type == "auction":
        # Falling back to the settling auction is what lets a closed piece still say who won.
        # get_running_auction covers draft/live/closing only, so once the sweep closed an
        # auction this returned nothing and the detail payload carried no auction fields at
        # all — the winner could never be told they had won, and a winner whose card was
        # declined had no way to see it.
        auction = auction_dao.get_running_auction(db, piece.id) or (
            auction_dao.get_settling_auction(db, piece.id)
        )
        base.update(bid_dao.bid_summary(db, auction, viewer_id=viewer_id))
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
        # Locked, not a plain read: this function can now move a piece out of `live`, which
        # races the auction close sweep for the same row.
        piece = db.execute(
            select(Piece).where(Piece.id == uuid.UUID(piece_id)).with_for_update()
        ).scalar_one_or_none()
        if not piece or piece.deleted_at is not None or piece.user_id != user.id:
            raise AppError("Piece not found.", 404)
        if "images" in body or "mediaUrls" in body:
            images = _extract_images(body)
            if not images:
                raise AppError("At least one image is required.", 400)
            for image in images:
                validate_user_media_url(user.username, image["mediaUrl"])
            replace_piece_media(db, piece.id, images)
            cover = images[0]
            piece.media_url = cover["mediaUrl"]
            piece.media_type = cover.get("mediaType", "image")
            if cover.get("mediaAspectRatio"):
                piece.media_aspect_ratio = cover["mediaAspectRatio"]
        elif "mediaUrl" in body and body["mediaUrl"]:
            validate_user_media_url(user.username, body["mediaUrl"])
            piece.media_url = body["mediaUrl"]
            # Legacy single-image patch — also updates just the cover row so
            # the gallery and the scalar cover field never disagree.
            existing_media = list_piece_media(db, piece.id)
            if existing_media:
                existing_media[0].media_url = body["mediaUrl"]
        for attr, key in [
            ("title", "title"), ("caption", "caption"), ("medium", "medium"),
            ("alt_text", "altText"), ("shipping_region", "shippingRegion"),
            ("location", "location"),
            ("year_created", "yearCreated"), ("framing_mounting", "framingMounting"),
            ("provenance", "provenance"), ("handling_notes", "handlingNotes"),
            ("weight_kg", "weightKg"), ("package_length_cm", "packageLengthCm"),
            ("package_width_cm", "packageWidthCm"), ("package_height_cm", "packageHeightCm"),
            ("declared_value_cents", "declaredValueCents"),
        ]:
            if key in body:
                setattr(piece, attr, body[key])
        if "materials" in body:
            piece.materials = body["materials"]
        if "styleTags" in body:
            piece.style_tags = body["styleTags"]
        if "dimensions" in body:
            piece.dimensions = body["dimensions"]

        # Listing terms move together or not at all. Previously each field was assigned
        # independently and only `isForSale` triggered validation, so {"listingType":
        # "banana"} was accepted and a live auction could be switched to fixed-price,
        # orphaning every bid against it.
        if any(field in body for field in listing_rules.COMMERCIAL_FIELDS):
            listing_rules.assert_terms_editable(db, piece)
            listing = _resolve_listing(db, body, piece, user)
            _validate_sale_detail_fields({**piece_to_dict(piece), **body})
            piece.is_for_sale = listing["is_for_sale"]
            piece.listing_type = listing["listing_type"]
            piece.price_cents = listing["price_cents"]

        if "status" in body:
            requested = (body.get("status") or "").strip().lower()
            # A whitelist, not a passthrough. reserved/sold/auction_won/deleted are driven
            # by money or by the auction closer; letting an owner set them by hand meant
            # they could hand themselves auction_won, which is the state auction_checkout
            # trusts to decide who may buy.
            if requested not in listing_rules.OWNER_DRIVEN_STATUSES:
                raise AppError(f"Status '{requested}' cannot be set directly.", 403)
            piece_state.transition_piece(db, piece, requested, commit=False)
        else:
            # Keep the clock consistent with whatever the listing fields now say, e.g. a
            # piece switched from auction to fixed must not keep a countdown.
            piece_state.apply_auction_clock(piece)
        db.commit()
        db.refresh(piece)
        return piece_to_dict(piece, media=list_piece_media(db, piece.id)), 200
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
        media_map = batch_list_piece_media(db, [p.id for p in pieces])
        return [piece_to_dict(p, media=media_map.get(p.id, [])) for p in pieces], 200
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
        media_map = batch_list_piece_media(db, piece_ids)
        author_ids = {p.user_id for p in pieces}
        authors = {
            u.id: u
            for u in db.execute(select(User).where(User.id.in_(author_ids))).scalars()
        } if author_ids else {}

        result = []
        for piece in pieces:
            base = piece_to_dict(piece, media=media_map.get(piece.id, []))
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
