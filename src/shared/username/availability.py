"""Username availability checks and caching."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from src.shared.config.redis_client import get_redis_client
from src.shared.models.user import User
from src.shared.models.username_history import UsernameHistory
from src.shared.username.constants import CACHE_KEY_PREFIX, CACHE_TTL_SECONDS
from src.shared.username.normalize import normalize
from src.shared.username.suggest import generate_candidates
from src.shared.username.validate import validate_blocked


@dataclass
class AvailabilityResult:
    available: bool
    normalized: str
    reason: str  # available | taken | reserved | invalid | blocked
    message: str
    suggestions: list[str]


def _cache_get(normalized: str) -> AvailabilityResult | None:
    try:
        raw = get_redis_client().get(f"{CACHE_KEY_PREFIX}{normalized}")
        if not raw:
            return None
        data = json.loads(raw)
        return AvailabilityResult(
            available=data["available"],
            normalized=normalized,
            reason=data["reason"],
            message=data["message"],
            suggestions=data.get("suggestions", []),
        )
    except Exception:
        return None


def _cache_set(result: AvailabilityResult) -> None:
    if result.reason == "invalid":
        return
    try:
        get_redis_client().set(
            f"{CACHE_KEY_PREFIX}{result.normalized}",
            json.dumps(
                {
                    "available": result.available,
                    "reason": result.reason,
                    "message": result.message,
                    "suggestions": result.suggestions,
                }
            ),
            ex=CACHE_TTL_SECONDS,
        )
    except Exception:
        pass


def invalidate_username_cache(normalized: str) -> None:
    try:
        get_redis_client().delete(f"{CACHE_KEY_PREFIX}{normalized}")
    except Exception:
        pass


def _is_taken_db(db: Session, normalized: str, exclude_user_id: uuid.UUID | None) -> tuple[bool, str]:
    user_q = select(exists().where(User.username == normalized))
    if exclude_user_id:
        user_q = select(
            exists().where(User.username == normalized, User.id != exclude_user_id)
        )

    reserved_q = select(
        exists().where(
            UsernameHistory.username == normalized,
            UsernameHistory.reserved_until > datetime.now(timezone.utc),
        )
    )
    if exclude_user_id:
        reserved_q = select(
            exists().where(
                UsernameHistory.username == normalized,
                UsernameHistory.reserved_until > datetime.now(timezone.utc),
                UsernameHistory.user_id != exclude_user_id,
            )
        )

    user_taken = db.execute(user_q).scalar()
    if user_taken:
        return True, "taken"

    reserved = db.execute(reserved_q).scalar()
    if reserved:
        return True, "reserved"

    return False, "available"


def batch_available(
    db: Session, candidates: list[str], exclude_user_id: uuid.UUID | None = None
) -> list[str]:
    if not candidates:
        return []
    now = datetime.now(timezone.utc)
    taken_users = set(
        db.execute(select(User.username).where(User.username.in_(candidates))).scalars().all()
    )
    if exclude_user_id:
        own = db.execute(select(User.username).where(User.id == exclude_user_id)).scalar_one_or_none()
        if own in taken_users:
            taken_users.discard(own)

    reserved = set(
        db.execute(
            select(UsernameHistory.username).where(
                UsernameHistory.username.in_(candidates),
                UsernameHistory.reserved_until > now,
            )
        ).scalars().all()
    )
    out = []
    for c in candidates:
        if validate_blocked(c):
            continue
        if c in taken_users or c in reserved:
            continue
        out.append(c)
    return out


def check_availability(
    db: Session,
    raw_username: str,
    exclude_user_id: uuid.UUID | None = None,
    include_suggestions: bool = True,
) -> AvailabilityResult:
    norm = normalize(raw_username)
    if not norm.ok or not norm.normalized:
        return AvailabilityResult(
            available=False,
            normalized=raw_username.strip().lower()[:30] if raw_username else "",
            reason="invalid",
            message="Username format is invalid.",
            suggestions=[],
        )

    normalized = norm.normalized

    if exclude_user_id:
        current = db.execute(select(User.username).where(User.id == exclude_user_id)).scalar_one_or_none()
        if current == normalized:
            return AvailabilityResult(
                available=True,
                normalized=normalized,
                reason="available",
                message="This is your current username.",
                suggestions=[],
            )

    cached = _cache_get(normalized)
    if cached and (cached.available or not include_suggestions):
        return cached

    blocked = validate_blocked(normalized)
    if blocked:
        result = AvailabilityResult(
            available=False,
            normalized=normalized,
            reason=blocked,
            message="This username isn't available.",
            suggestions=[],
        )
        _cache_set(result)
        return result

    taken, reason = _is_taken_db(db, normalized, exclude_user_id)
    if taken:
        suggestions: list[str] = []
        if include_suggestions:
            candidates = generate_candidates(normalized)
            suggestions = batch_available(db, candidates, exclude_user_id)[:5]
        result = AvailabilityResult(
            available=False,
            normalized=normalized,
            reason=reason,
            message="This username isn't available.",
            suggestions=suggestions,
        )
        _cache_set(result)
        return result

    result = AvailabilityResult(
        available=True,
        normalized=normalized,
        reason="available",
        message="Username is available.",
        suggestions=[],
    )
    _cache_set(result)
    return result
