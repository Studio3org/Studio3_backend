"""Local-disk media fallback for development without S3 configured.

The upload URL is signed the way an S3 presigned PUT is: it names one key, one content type
and a byte limit, and expires. The routes answer only in development and testing, and only
while S3 is unconfigured — a deployed environment must have a bucket (settings.py).
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

_BASE_DIR = Path(__file__).resolve().parents[3]
UPLOAD_DIR = Path(os.getenv("LOCAL_MEDIA_DIR") or (_BASE_DIR / "uploads"))

# Matches the hour an S3 presigned PUT stays valid for (s3_service.presign_put).
UPLOAD_URL_TTL_SECONDS = 3600
_LOCAL_ENVIRONMENTS = ("development", "testing")


def local_media_enabled() -> bool:
    from src.shared.storage.s3_client import s3_configured

    return os.getenv("FLASK_ENV", "development") in _LOCAL_ENVIRONMENTS and not s3_configured()


def _resolve(key: str) -> Path:
    path = (UPLOAD_DIR / key).resolve()
    if UPLOAD_DIR.resolve() not in path.parents and path != UPLOAD_DIR.resolve():
        raise ValueError("Invalid media key.")
    return path


def _signature(secret: str, key: str, content_type: str, max_bytes: int, expires: int) -> str:
    message = f"{key}\n{content_type}\n{max_bytes}\n{expires}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_upload_query(secret: str, key: str, content_type: str, max_bytes: int) -> str:
    """Query string that authorises one PUT of `key`, like the signature on an S3 URL."""
    expires = int(time.time()) + UPLOAD_URL_TTL_SECONDS
    sig = _signature(secret, key, content_type, max_bytes, expires)
    return urlencode({"ct": content_type, "max": max_bytes, "exp": expires, "sig": sig})


def verify_upload(
    secret: str, key: str, args, content_type: Optional[str], length: Optional[int]
) -> bool:
    """True when the request carries a live signature for exactly this key, type and size."""
    try:
        signed_type = args["ct"]
        max_bytes = int(args["max"])
        expires = int(args["exp"])
        sig = args["sig"]
    except (KeyError, ValueError):
        return False
    if expires < time.time():
        return False
    expected = _signature(secret, key, signed_type, max_bytes, expires)
    if not hmac.compare_digest(expected, sig):
        return False
    if (content_type or "").split(";")[0].strip().lower() != signed_type:
        return False
    return length is not None and 0 < length <= max_bytes


def save_local_file(key: str, data: bytes) -> None:
    path = _resolve(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def read_local_file(key: str) -> Optional[bytes]:
    path = _resolve(key)
    if not path.is_file():
        return None
    return path.read_bytes()


def guess_content_type(key: str) -> str:
    return mimetypes.guess_type(key)[0] or "application/octet-stream"
