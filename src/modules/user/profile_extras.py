"""Profile extras for the profile boards (BACKEND_CHANGES B31 mediums, B34 Private Collection,
B36 a private profile's header).

- `profileMediums`: up to three mediums the artist picked in Edit profile, in order (the first
  is their main one), shown as one line under the username. Not `mediums`, which is the taste
  picked at sign-up (`taste_preferences.mediums`).
- `collectionPrivate`: the owner hides their Collection from everyone else, followers too.
- `locked_header`: what someone who can't see a private account still sees (`ProfilePrivate`):
  photo, cover, name, username, bio, following count and stats, and whether Message shows.
  Never the followers or pieces counts: the app reads a locked profile by their absence.
"""
from src.shared.utils.app_error import AppError

MAX_PROFILE_MEDIUMS = 3
MAX_MEDIUM_LENGTH = 40


def clean_profile_mediums(value) -> list:
    """The mediums as sent, trimmed, without blanks or repeats (case-insensitive), in order.
    More than three, or anything but a list of strings, is refused."""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(m, str) for m in value):
        raise AppError("profileMediums must be a list of strings.", 400)
    out, seen = [], set()
    for raw in value:
        medium = " ".join(raw.split())
        if not medium or medium.lower() in seen:
            continue
        if len(medium) > MAX_MEDIUM_LENGTH:
            raise AppError(f"Each medium must be {MAX_MEDIUM_LENGTH} characters or fewer.", 400)
        seen.add(medium.lower())
        out.append(medium)
    if len(out) > MAX_PROFILE_MEDIUMS:
        raise AppError(f"Pick up to {MAX_PROFILE_MEDIUMS} mediums.", 400)
    return out


def clean_collection_private(value) -> bool:
    if not isinstance(value, bool):
        raise AppError("collectionPrivate must be true or false.", 400)
    return value


def collection_visible(user, viewer_id) -> bool:
    """A private Collection is the owner's alone."""
    return not getattr(user, "collection_private", False) or viewer_id == user.id


def locked_header(
    user,
    *,
    is_pending: bool,
    following_count: int,
    collected_count: int,
    artists_count: int,
    saves_count: int,
) -> dict:
    """The header a private account shows someone who doesn't follow it."""
    return {
        "username": user.username,
        "name": user.name,
        "profilePhotoUrl": user.image,
        "coverPhotoUrl": user.cover_photo_url,
        "bio": user.bio,
        "role": user.role,
        "profileVisibility": "private",
        "isFollowing": False,
        "followRequestPending": is_pending,
        "followingCount": following_count,
        "collectedCount": collected_count,
        "artistsCount": artists_count,
        "savesCount": saves_count,
        # Message shows only when they take messages from everyone.
        "messagePermission": user.message_permission,
    }


