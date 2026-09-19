"""CRUD for refresh_tokens table (SQLAlchemy)."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from src.shared.models.refresh_token import RefreshToken


def create(
    db: Session,
    user_id: uuid.UUID,
    hashed_token: str,
    session_id: str,
    expires_at: datetime,
) -> RefreshToken:
    row = RefreshToken(
        id=uuid.uuid4(),
        user_id=user_id,
        hashed_token=hashed_token,
        session_id=session_id,
        is_revoked=False,
        expires_at=expires_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def find_by_id(db: Session, token_id: uuid.UUID) -> Optional[RefreshToken]:
    return db.get(RefreshToken, token_id)


def revoke_by_id(db: Session, token_id: uuid.UUID) -> None:
    db.execute(update(RefreshToken).where(RefreshToken.id == token_id).values(is_revoked=True))
    db.commit()


def revoke_all_for_user(db: Session, user_id: uuid.UUID) -> None:
    db.execute(update(RefreshToken).where(RefreshToken.user_id == user_id).values(is_revoked=True))
    db.commit()
