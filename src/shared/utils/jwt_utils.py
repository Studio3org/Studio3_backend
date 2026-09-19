"""JWT sign and verify using secret from env."""
import os
from typing import Optional

import jwt
from datetime import datetime, timedelta, timezone

JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production")
JWT_ACCESS_EXPIRY_MINUTES = int(os.getenv("JWT_ACCESS_EXPIRY_MINUTES", "15"))


def sign_access_token(payload: dict) -> str:
    """Sign payload (e.g. sub, sessionId) with expiry; return JWT string."""
    now = datetime.now(timezone.utc)
    payload = dict(payload)
    payload["iat"] = now
    payload["exp"] = now + timedelta(minutes=JWT_ACCESS_EXPIRY_MINUTES)
    return jwt.encode(
        payload,
        JWT_SECRET,
        algorithm="HS256",
    )


def verify_access_token(token: str) -> Optional[dict]:
    """Verify JWT and return payload or None if invalid/expired."""
    try:
        return jwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
        )
    except jwt.InvalidTokenError:
        return None
