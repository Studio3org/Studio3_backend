"""User profile and onboarding DAO."""
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from src.shared.models.user import User


def get_user_by_id(db: Session, user_id: uuid.UUID) -> Optional[User]:
    return db.get(User, user_id)


def update_user_fields(db: Session, user: User, **fields) -> User:
    for k, v in fields.items():
        if hasattr(user, k):
            setattr(user, k, v)
    db.commit()
    db.refresh(user)
    return user



