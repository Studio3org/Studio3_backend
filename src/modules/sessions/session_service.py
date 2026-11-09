"""Redis session: create, find, delete. Key session:<session_id>, no expiry —
a session lives until explicit logout/revocation, never on its own."""
import json
import uuid
from datetime import datetime, timezone

from src.shared.config.redis_client import get_redis_client


def create_session(user_id: str, user_agent: str = "", ip: str = "") -> str:
    """Create Redis session; return session_id."""
    session_id = str(uuid.uuid4())
    key = f"session:{session_id}"
    value = json.dumps({
        "userId": user_id,
        "userAgent": user_agent or "",
        "ip": ip or "",
        "createdAt": datetime.now(timezone.utc).isoformat(),
    })
    r = get_redis_client()
    r.set(key, value)
    return session_id


def find_session(session_id: str):
    """Get session data or None if missing/expired."""
    r = get_redis_client()
    raw = r.get(f"session:{session_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def delete_session(session_id: str) -> None:
    """Remove one session from Redis."""
    r = get_redis_client()
    r.delete(f"session:{session_id}")


def delete_all_sessions_for_user(user_id: str, keep_session_id: str | None = None) -> None:
    """Scan keys session:* and remove those whose value contains this user_id. O(n) over keys.

    `keep_session_id` spares one session: "log out of all devices" keeps the device asking
    (B34). Password resets and account deletion pass nothing and sign everything out."""
    r = get_redis_client()
    pattern = "session:*"
    keep_key = f"session:{keep_session_id}" if keep_session_id else None
    for key in r.scan_iter(match=pattern):
        if keep_key and (key.decode() if isinstance(key, bytes) else key) == keep_key:
            continue
        try:
            raw = r.get(key)
            if raw:
                data = json.loads(raw)
                if data.get("userId") == user_id:
                    r.delete(key)
        except Exception:
            continue
