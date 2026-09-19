"""Transactional username claim and change."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.shared.models.user import User
from src.shared.models.username_history import UsernameHistory
from src.shared.username.availability import check_availability, invalidate_username_cache
from src.shared.username.constants import USERNAME_CHANGE_COOLDOWN_DAYS
from src.shared.username.normalize import normalize
from src.shared.utils.app_error import AppError


def claim_username(db: Session, user_id: uuid.UUID, raw_username: str) -> str:
    """Set username on user row; raises AppError on conflict."""
    norm = normalize(raw_username)
    if not norm.ok or not norm.normalized:
        raise AppError("Invalid username format.", 400)

    avail = check_availability(db, norm.normalized, include_suggestions=True)
    if not avail.available:
        if avail.reason == "taken":
            raise AppError("Username is already taken.", 409)
        if avail.reason == "reserved":
            raise AppError("Username is reserved.", 409)
        raise AppError("Username is not available.", 400)

    try:
        db.execute(update(User).where(User.id == user_id).values(username=norm.normalized))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError("Username was just taken. Try another.", 409)

    invalidate_username_cache(norm.normalized)
    return norm.normalized


def change_username(db: Session, user: User, raw_new: str) -> User:
    """Instagram-style in-place username change with history reserve."""
    if user.last_username_change_at:
        next_allowed = user.last_username_change_at + timedelta(days=USERNAME_CHANGE_COOLDOWN_DAYS)
        if datetime.now(timezone.utc) < next_allowed:
            days_left = (next_allowed - datetime.now(timezone.utc)).days + 1
            raise AppError(f"You can change your username again in {days_left} day(s).", 429)

    norm = normalize(raw_new)
    if not norm.ok or not norm.normalized:
        raise AppError("Invalid username format.", 400)

    if user.username == norm.normalized:
        return user

    avail = check_availability(db, norm.normalized, exclude_user_id=user.id, include_suggestions=True)
    if not avail.available:
        msg = "This username isn't available."
        if avail.suggestions:
            msg += f" Try: {', '.join(avail.suggestions[:3])}"
        raise AppError(msg, 409)

    old_username = user.username
    reserved_until = datetime.now(timezone.utc) + timedelta(days=USERNAME_CHANGE_COOLDOWN_DAYS)
    now = datetime.now(timezone.utc)

    try:
        db.add(
            UsernameHistory(
                id=uuid.uuid4(),
                user_id=user.id,
                username=old_username,
                reserved_until=reserved_until,
                created_at=now,
            )
        )
        user.username = norm.normalized
        user.last_username_change_at = now
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        raise AppError("Username was just taken. Try another.", 409)

    invalidate_username_cache(old_username)
    invalidate_username_cache(norm.normalized)
    return user
