"""User profile, onboarding, seller, username change."""
import uuid

from flask import request, g, redirect, url_for

from src.shared.config.database import SessionLocal
from src.shared.constants import ALLOWED_ROLES
from src.shared.storage.s3_service import validate_user_media_url, migrate_user_prefix
from src.shared.username.claim import change_username
from src.shared.username.constants import RATE_USERNAME_CHANGE_PER_USER
from src.shared.utils.app_error import AppError
from src.shared.utils.rate_limit import rate_limit_user
from src.modules.auth.auth_dao import find_user_by_username_or_history
from src.modules.user.user_dao import get_user_by_id, update_user_fields, delist_user_pieces
from src.modules.user.user_serializers import user_to_dict
from src.modules.pieces.pieces_dao import list_user_pieces, get_piece
from src.modules.social import social_dao
from src.modules.social import block_dao
from src.modules.user import device_dao


def _resolve_target_owner(db, target_type: str, target_id: str):
    from src.modules.posts.posts_dao import get_post

    try:
        tid = uuid.UUID(target_id)
    except (ValueError, TypeError):
        return None
    if target_type == "piece":
        piece = get_piece(db, tid)
        return piece.user_id if piece else None
    if target_type == "post":
        post = get_post(db, tid)
        return post.user_id if post else None
    return None


def get_me():
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        if not user:
            raise AppError("User not found.", 404)
        return user_to_dict(db, user, viewer_id=user.id), 200
    finally:
        db.close()


def patch_me():
    body = request.get_json() or {}
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        if not user:
            raise AppError("User not found.", 404)
        fields = {}
        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name:
                raise AppError("Name is required.", 400)
            fields["name"] = name
        if "bio" in body:
            bio = body.get("bio")
            if bio and len(bio) > 250:
                raise AppError("Bio must be 250 characters or fewer.", 400)
            fields["bio"] = bio
        if "location" in body:
            fields["location"] = body.get("location")
        if "phone" in body:
            fields["phone"] = (body.get("phone") or "").strip() or None
        if "pronouns" in body:
            fields["pronouns"] = body.get("pronouns")
        if "website" in body:
            website = (body.get("website") or "").strip() or None
            if website and len(website) > 500:
                raise AppError("Website must be 500 characters or fewer.", 400)
            fields["website"] = website
        if "instagram" in body:
            instagram = (body.get("instagram") or "").strip().lstrip("@") or None
            if instagram and len(instagram) > 100:
                raise AppError("Instagram handle must be 100 characters or fewer.", 400)
            fields["instagram"] = instagram
        if "twitter" in body:
            twitter = (body.get("twitter") or "").strip().lstrip("@") or None
            if twitter and len(twitter) > 100:
                raise AppError("Twitter/X handle must be 100 characters or fewer.", 400)
            fields["twitter"] = twitter
        if "category" in body:
            category = (body.get("category") or "").strip() or None
            if category and len(category) > 50:
                raise AppError("Category must be 50 characters or fewer.", 400)
            fields["category"] = category
        if "tags" in body:
            tags = body.get("tags") or []
            if not isinstance(tags, list) or len(tags) > 8 or any(not isinstance(t, str) for t in tags):
                raise AppError("tags must be a list of at most 8 strings.", 400)
            fields["tags"] = [t.strip().lstrip("#") for t in tags if t.strip()]
        if "mediums" in body:
            taste = dict(user.taste_preferences or {})
            taste["mediums"] = body.get("mediums") or []
            fields["taste_preferences"] = taste
        if "profilePhotoUrl" in body:
            url = body.get("profilePhotoUrl")
            if url:
                validate_user_media_url(user.username, url)
            fields["image"] = url
        if "coverPhotoUrl" in body:
            url = body.get("coverPhotoUrl")
            if url:
                validate_user_media_url(user.username, url)
            fields["cover_photo_url"] = url
        if "latitude" in body or "longitude" in body:
            lat, lng = body.get("latitude"), body.get("longitude")
            if (lat is None) != (lng is None):
                raise AppError("Both latitude and longitude are required together.", 400)
            fields["latitude"] = lat
            fields["longitude"] = lng
        if "bannerTargetType" in body or "bannerTargetId" in body:
            target_type = body.get("bannerTargetType")
            target_id = body.get("bannerTargetId")
            if target_type is None and target_id is None:
                fields["banner_target_type"] = None
                fields["banner_target_id"] = None
            else:
                if target_type not in ("piece", "post") or not target_id:
                    raise AppError("bannerTargetType must be 'piece' or 'post' with a bannerTargetId.", 400)
                owner_id = _resolve_target_owner(db, target_type, target_id)
                if owner_id != user.id:
                    raise AppError(f"{target_type.capitalize()} not found.", 404)
                fields["banner_target_type"] = target_type
                fields["banner_target_id"] = uuid.UUID(target_id)
        if "bannerAutoRule" in body:
            rule = body.get("bannerAutoRule")
            if rule not in ("most_saved", "most_recent", "none"):
                raise AppError("bannerAutoRule must be one of: most_saved, most_recent, none.", 400)
            fields["banner_auto_rule"] = rule
        if "messagePermission" in body:
            perm = body.get("messagePermission")
            if perm not in ("everyone", "following", "no_one"):
                raise AppError("messagePermission must be one of: everyone, following, no_one.", 400)
            fields["message_permission"] = perm
        if "profileVisibility" in body:
            visibility = body.get("profileVisibility")
            if visibility not in ("public", "private"):
                raise AppError("profileVisibility must be 'public' or 'private'.", 400)
            fields["profile_visibility"] = visibility
        user = update_user_fields(db, user, **fields)
        return user_to_dict(db, user, viewer_id=user.id), 200
    finally:
        db.close()


def patch_username():
    rate_limit_user("username_change", g.user["id"], RATE_USERNAME_CHANGE_PER_USER, 3600)
    body = request.get_json() or {}
    new_username = (body.get("username") or "").strip()
    if not new_username:
        raise AppError("Username is required.", 400)

    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        if not user:
            raise AppError("User not found.", 404)
        old_username = user.username
        user = change_username(db, user, new_username)
        if old_username != user.username:
            migrate_user_prefix(db, user.id, old_username, user.username)
        return user_to_dict(db, user, viewer_id=user.id), 200
    finally:
        db.close()


def get_public_profile(username: str):
    db = SessionLocal()
    try:
        user, is_redirect = find_user_by_username_or_history(db, username.lower())
        if not user:
            raise AppError("User not found.", 404)
        viewer_id = uuid.UUID(g.user["id"]) if getattr(g, "user", None) else None

        if viewer_id and block_dao.is_blocked_either_way(db, viewer_id, user.id):
            raise AppError("User not found.", 404)

        is_owner = viewer_id == user.id
        is_follower = social_dao.user_follows(db, viewer_id, user.id) if viewer_id else False
        if user.profile_visibility == "private" and not is_owner and not is_follower:
            # Locked profile header only — pieces/posts/series grids are gated separately
            # in each list endpoint via social_dao.can_view_content().
            is_pending = social_dao.has_pending_request(db, viewer_id, user.id) if viewer_id else False
            return {
                "username": user.username,
                "name": user.name,
                "profilePhotoUrl": user.image,
                "profileVisibility": "private",
                "isFollowing": is_follower,
                "followRequestPending": is_pending,
            }, 200

        data = user_to_dict(db, user, include_private=False, viewer_id=viewer_id)
        if is_redirect:
            data["redirectToUsername"] = user.username
        if not is_owner:
            data["followRequestPending"] = (
                social_dao.has_pending_request(db, viewer_id, user.id) if viewer_id else False
            )
        return data, 200
    finally:
        db.close()


def patch_role():
    body = request.get_json() or {}
    role = (body.get("role") or "").strip().lower()
    if role not in ALLOWED_ROLES:
        raise AppError(f"Role must be one of: {', '.join(ALLOWED_ROLES)}", 400)
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        user = update_user_fields(db, user, role=role)
        return user_to_dict(db, user, viewer_id=user.id), 200
    finally:
        db.close()


def onboarding_preferences():
    body = request.get_json() or {}
    mediums = body.get("mediums") or []
    styles = body.get("styles") or []
    themes = body.get("themes") or []
    if len(mediums) < 3 or len(styles) < 3 or len(themes) < 3:
        raise AppError("Select at least 3 mediums, 3 styles, and 3 themes.", 400)
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        user = update_user_fields(
            db,
            user,
            taste_preferences={"mediums": mediums, "styles": styles, "themes": themes},
        )
        return user_to_dict(db, user, viewer_id=user.id), 200
    finally:
        db.close()


def onboarding_photos():
    body = request.get_json() or {}
    if body.get("skip"):
        return get_me()
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        fields = {}
        if body.get("profilePhotoUrl"):
            validate_user_media_url(user.username, body["profilePhotoUrl"])
            fields["image"] = body["profilePhotoUrl"]
        if body.get("coverPhotoUrl"):
            validate_user_media_url(user.username, body["coverPhotoUrl"])
            fields["cover_photo_url"] = body["coverPhotoUrl"]
        if fields:
            user = update_user_fields(db, user, **fields)
        return user_to_dict(db, user, viewer_id=user.id), 200
    finally:
        db.close()


def onboarding_complete():
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        if not user.role:
            raise AppError("Set your role before completing onboarding.", 400)
        if not user.taste_preferences:
            raise AppError("Set your preferences before completing onboarding.", 400)
        user = update_user_fields(db, user, onboarding_complete=True)
        return user_to_dict(db, user, viewer_id=user.id), 200
    finally:
        db.close()


def seller_enable():
    body = request.get_json() or {}
    location = (body.get("location") or "").strip()
    if not location and not body.get("useProfileLocation"):
        raise AppError("Seller location is required.", 400)
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        if body.get("useProfileLocation") and user.location:
            location = user.location
        user = update_user_fields(db, user, seller_enabled=True, location=location or user.location)
        return {"sellerEnabled": True, "location": user.location}, 200
    finally:
        db.close()


def seller_disable():
    from src.modules.orders import orders_dao

    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        # Block deactivation while anything is "in flight" — never auto-delist/abandon.
        active_listings = list_user_pieces(db, user.id, for_sale_only=True)
        if active_listings:
            raise AppError(
                "Remove your active listings before disabling seller mode.", 400
            )
        in_progress = orders_dao.count_seller_in_progress(db, user.id)
        if in_progress:
            raise AppError(
                "You have in-progress sales — complete or cancel them before disabling seller mode.",
                400,
            )
        user = update_user_fields(db, user, seller_enabled=False)
        # Defensive no-op by this point: the checks above guarantee no active listings
        # remain, so this has nothing left to delist. Kept as a safety net, not the
        # primary enforcement (that's the pre-check above).
        delist_user_pieces(db, user.id)
        return {"sellerEnabled": False}, 200
    finally:
        db.close()


def seller_status():
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        return {"sellerEnabled": user.seller_enabled, "location": user.location}, 200
    finally:
        db.close()


def seller_analytics():
    from src.modules.orders import orders_dao
    from src.modules.inquiries import inquiries_dao

    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        piece_ids = [p.id for p in list_user_pieces(db, user.id)]
        return {
            "savesCount": social_dao.count_saves_for_targets(db, "piece", piece_ids),
            "likesCount": social_dao.count_likes_for_targets(db, "piece", piece_ids),
            "inquiriesCount": inquiries_dao.count_seller_inquiries(db, user.id),
            "salesCount": orders_dao.count_seller_sales(db, user.id),
            "period": "all_time",
        }, 200
    finally:
        db.close()


ALLOWED_PLATFORMS = ("ios", "android", "web")


def register_device():
    body = request.get_json() or {}
    platform = (body.get("platform") or "").strip().lower()
    push_token = (body.get("pushToken") or "").strip()
    if platform not in ALLOWED_PLATFORMS:
        raise AppError(f"platform must be one of: {', '.join(ALLOWED_PLATFORMS)}", 400)
    if not push_token:
        raise AppError("pushToken is required.", 400)
    db = SessionLocal()
    try:
        device_dao.upsert_device(db, uuid.UUID(g.user["id"]), platform, push_token)
        return {"registered": True}, 200
    finally:
        db.close()


EARTH_RADIUS_KM = 6371.0


def nearby_users():
    from sqlalchemy import func, select
    from src.shared.models.user import User

    try:
        lat = float(request.args["lat"])
        lng = float(request.args["lng"])
    except (KeyError, ValueError):
        raise AppError("lat and lng query params are required.", 400)
    radius_km = float(request.args.get("radiusKm", 50))
    limit = min(int(request.args.get("limit", 20)), 50)

    db = SessionLocal()
    try:
        viewer_id = uuid.UUID(g.user["id"]) if getattr(g, "user", None) else None
        # Haversine via Postgres native trig functions — no PostGIS extension available
        # on this instance. Full-table scan (the expression isn't index-usable); fine at
        # hundreds/low-thousands of sellers. If this becomes slow, add a bounding-box
        # pre-filter on indexed lat/lng ranges before this, or migrate to real PostGIS.
        distance_expr = (
            EARTH_RADIUS_KM
            * func.acos(
                func.cos(func.radians(lat))
                * func.cos(func.radians(User.latitude))
                * func.cos(func.radians(User.longitude) - func.radians(lng))
                + func.sin(func.radians(lat)) * func.sin(func.radians(User.latitude))
            )
        ).label("distance_km")

        q = (
            select(User, distance_expr)
            .where(User.latitude.is_not(None), User.longitude.is_not(None), User.seller_enabled.is_(True))
            .where(distance_expr <= radius_km)
            .order_by(distance_expr.asc())
            .limit(limit)
        )
        rows = db.execute(q).all()
        items = []
        for user, distance_km in rows:
            data = user_to_dict(db, user, include_private=False, viewer_id=viewer_id)
            data["distanceKm"] = round(distance_km, 1)
            items.append(data)
        return {"items": items}, 200
    finally:
        db.close()


def unregister_device():
    body = request.get_json() or {}
    push_token = (body.get("pushToken") or "").strip()
    if not push_token:
        raise AppError("pushToken is required.", 400)
    db = SessionLocal()
    try:
        device_dao.delete_device_by_token(db, push_token)
        return {"registered": False}, 200
    finally:
        db.close()


DEFAULT_NOTIFICATION_PREFERENCES = {
    "push": {"follow": True, "like": True, "save": True, "comment": True, "inquiry": True, "message": True, "purchase": True},
    "dailyDigest": {"enabled": False, "time": "09:00"},
}


def update_notification_preferences():
    """Partial update, deep-merged into the existing JSONB blob (same shape/merge convention
    as the `mediums` alias into taste_preferences — see patch_me()).

    Note: dailyDigest is a stored setting only — no scheduled job reads/sends it yet. No task
    scheduler exists in this codebase (same category of decision as the push-queue question
    declined earlier); this just persists the preference for when that lands."""
    body = request.get_json() or {}
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        current = user.notification_preferences or DEFAULT_NOTIFICATION_PREFERENCES
        merged = {**DEFAULT_NOTIFICATION_PREFERENCES, **current}

        if "push" in body:
            if not isinstance(body["push"], dict):
                raise AppError("push must be an object of type -> boolean.", 400)
            merged["push"] = {**merged.get("push", {}), **body["push"]}
        if "dailyDigest" in body:
            if not isinstance(body["dailyDigest"], dict):
                raise AppError("dailyDigest must be an object.", 400)
            merged["dailyDigest"] = {**merged.get("dailyDigest", {}), **body["dailyDigest"]}

        user = update_user_fields(db, user, notification_preferences=merged)
        return {"notificationPreferences": user.notification_preferences}, 200
    finally:
        db.close()
