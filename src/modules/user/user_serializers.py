"""User API serializers."""
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from src.shared.models.user import User
from src.shared.username.constants import USERNAME_CHANGE_COOLDOWN_DAYS


def can_change_username(user: User) -> bool:
    if not user.last_username_change_at:
        return True
    next_allowed = user.last_username_change_at + timedelta(days=USERNAME_CHANGE_COOLDOWN_DAYS)
    return datetime.now(timezone.utc) >= next_allowed


def _compute_banner(db: Session, user: User) -> Optional[dict]:
    """Resolve the profile's "magnum opus" banner — either a manually-pinned piece/post,
    or one computed at read time per banner_auto_rule. No cron job materializes this;
    it's cheap enough to compute per profile read (same tradeoff as the haversine "near me"
    query — simplicity over pre-computation, revisit only if it becomes a hot path)."""
    from src.modules.pieces.pieces_dao import list_user_pieces, get_piece
    from src.modules.posts.posts_dao import list_user_posts, get_post
    from src.modules.social import social_dao

    if user.banner_target_type and user.banner_target_id:
        if user.banner_target_type == "piece":
            piece = get_piece(db, user.banner_target_id)
            if piece and piece.user_id == user.id:
                return {"targetType": "piece", "targetId": str(piece.id), "mediaUrl": piece.media_url}
        elif user.banner_target_type == "post":
            post = get_post(db, user.banner_target_id)
            if post and post.user_id == user.id:
                return {"targetType": "post", "targetId": str(post.id), "mediaUrl": post.media_url}
        return None  # manually pinned target no longer exists/owned — fall through to no banner

    if user.banner_auto_rule == "none":
        return None

    pieces = list_user_pieces(db, user.id)
    posts = list_user_posts(db, user.id)
    if not pieces and not posts:
        return None

    if user.banner_auto_rule == "most_recent":
        candidates = [("piece", p.id, p.media_url, p.created_at) for p in pieces] + [
            ("post", p.id, p.media_url, p.created_at) for p in posts
        ]
        best = max(candidates, key=lambda c: c[3])
        return {"targetType": best[0], "targetId": str(best[1]), "mediaUrl": best[2]}

    if user.banner_auto_rule == "most_saved":
        piece_saves = social_dao.batch_save_counts(db, "piece", [p.id for p in pieces])
        post_saves = social_dao.batch_save_counts(db, "post", [p.id for p in posts])
        candidates = [("piece", p.id, p.media_url, piece_saves.get(p.id, 0)) for p in pieces] + [
            ("post", p.id, p.media_url, post_saves.get(p.id, 0)) for p in posts
        ]
        best = max(candidates, key=lambda c: c[3])
        if best[3] == 0:
            return None  # nothing saved yet — don't show an arbitrary pick as "most saved"
        return {"targetType": best[0], "targetId": str(best[1]), "mediaUrl": best[2]}

    return None


def user_to_dict(
    db: Session, user: User, include_private: bool = True, viewer_id: Optional[uuid.UUID] = None
) -> dict:
    from src.modules.social import social_dao
    from src.modules.pieces.pieces_dao import list_user_pieces, list_saved_pieces, piece_to_dict
    from src.modules.orders import orders_dao

    data = {
        "username": user.username,
        "name": user.name,
        "profilePhotoUrl": user.image,
        "coverPhotoUrl": user.cover_photo_url,
        "bio": user.bio,
        "location": user.location,
        "pronouns": user.pronouns,
        "website": user.website,
        "instagram": user.instagram,
        "twitter": user.twitter,
        "category": user.category,
        "tags": user.tags or [],
        "mediums": (user.taste_preferences or {}).get("mediums", []),
        "role": user.role,
        "sellerEnabled": user.seller_enabled,
        "onboardingComplete": user.onboarding_complete,
        "banner": _compute_banner(db, user),
    }

    data["followersCount"] = social_dao.count_followers(db, user.id)
    data["followingCount"] = social_dao.count_following(db, user.id)
    data["piecesCount"] = len(list_user_pieces(db, user.id))
    data["savesCount"] = social_dao.count_user_saves(db, user.id)
    data["collectedCount"] = orders_dao.count_buyer_collected(db, user.id)
    data["isFollowing"] = social_dao.user_follows(db, viewer_id, user.id) if viewer_id else False

    # Flutter client documented fallback aliases (see BACKEND_API_README.md)
    data["following"] = data["followingCount"]
    data["followers"] = data["followersCount"]
    data["pieces"] = data["piecesCount"]
    data["saves"] = data["savesCount"]
    data["collected"] = data["collectedCount"]
    data["isSeller"] = data["sellerEnabled"]

    if include_private:
        saved_pieces = [piece_to_dict(p) for p in list_saved_pieces(db, user.id)[:20]]
        data.update(
            {
                "email": user.email,
                "phone": user.phone,
                "emailVerified": user.email_verified,
                "lastUsernameChangeAt": user.last_username_change_at.isoformat() if user.last_username_change_at else None,
                "canChangeUsername": can_change_username(user),
                "tastePreferences": user.taste_preferences,
                "savedPieces": saved_pieces,
                "bannerTargetType": user.banner_target_type,
                "bannerTargetId": str(user.banner_target_id) if user.banner_target_id else None,
                "bannerAutoRule": user.banner_auto_rule,
                "messagePermission": user.message_permission,
                "profileVisibility": user.profile_visibility,
                "notificationPreferences": user.notification_preferences,
            }
        )
        data["saved"] = saved_pieces
    return data
