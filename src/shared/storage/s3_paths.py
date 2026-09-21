"""S3 path helpers."""

from __future__ import annotations

import mimetypes
import uuid

PURPOSE_DIRS = {
    "profile": "profile",
    "cover": "cover",
    "piece": "pieces",
    "post": "posts",
    "chat": "chat",
    "event": "events",
}


def ext_for_content_type(content_type: str) -> str:
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".bin"
    if ext == ".jpe":
        ext = ".jpg"
    return ext.lstrip(".")


def user_key(username: str, *parts: str) -> str:
    return "/".join([username.lower(), *parts])


def build_media_key(
    username: str,
    purpose: str,
    content_type: str,
    content_id: str | None = None,
) -> str:
    ext = ext_for_content_type(content_type)
    folder = PURPOSE_DIRS.get(purpose, purpose)
    if purpose in ("profile", "cover"):
        # One fixed object per user, deliberately: a profile has exactly one avatar and one
        # banner, and replacing them should not leave the old bytes behind.
        #
        # Nothing that can exist more than once per user may use these. Event covers did,
        # which meant every event a host published overwrote their profile banner and then
        # displayed it as the event's own — the same S3 object under two names.
        filename = "avatar" if purpose == "profile" else "banner"
        return user_key(username, folder, f"{filename}.{ext}")
    cid = content_id or str(uuid.uuid4())
    return user_key(username, folder, cid, f"original.{ext}")


def public_url(base_url: str, key: str) -> str:
    return f"{base_url.rstrip('/')}/{key}"
