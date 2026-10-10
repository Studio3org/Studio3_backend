"""Blocked-accounts DAO."""
import uuid

from sqlalchemy import select, or_, and_, delete
from sqlalchemy.orm import Session

from src.shared.models.block import Block
from src.shared.models.social import Follow
from src.shared.models.user import User


def block_user(db: Session, blocker_id: uuid.UUID, blocked_id: uuid.UUID) -> Block:
    existing = db.execute(
        select(Block).where(Block.blocker_id == blocker_id, Block.blocked_id == blocked_id)
    ).scalar_one_or_none()
    if existing:
        return existing
    block = Block(id=uuid.uuid4(), blocker_id=blocker_id, blocked_id=blocked_id)
    db.add(block)
    # Blocking severs any existing follow relationship in either direction.
    db.execute(
        delete(Follow).where(
            or_(
                and_(Follow.follower_id == blocker_id, Follow.following_id == blocked_id),
                and_(Follow.follower_id == blocked_id, Follow.following_id == blocker_id),
            )
        )
    )
    db.commit()
    db.refresh(block)
    return block


def unblock_user(db: Session, blocker_id: uuid.UUID, blocked_id: uuid.UUID) -> bool:
    result = db.execute(
        delete(Block).where(Block.blocker_id == blocker_id, Block.blocked_id == blocked_id)
    )
    db.commit()
    return result.rowcount > 0


def is_blocked_either_way(db: Session, user_a: uuid.UUID, user_b: uuid.UUID) -> bool:
    return db.execute(
        select(Block.id).where(
            or_(
                and_(Block.blocker_id == user_a, Block.blocked_id == user_b),
                and_(Block.blocker_id == user_b, Block.blocked_id == user_a),
            )
        )
    ).first() is not None


def blocked_user_ids(db: Session, viewer_id: uuid.UUID | None) -> set[uuid.UUID]:
    """Every account hidden from [viewer_id]: ones they blocked and ones that blocked them.

    Used to strip those accounts' pieces, scenes, and comments out of feeds and comment
    threads, so blocking someone removes their content from the blocker's app immediately.
    """
    if not viewer_id:
        return set()
    rows = db.execute(
        select(Block.blocker_id, Block.blocked_id).where(
            or_(Block.blocker_id == viewer_id, Block.blocked_id == viewer_id)
        )
    ).all()
    return {blocked if blocker == viewer_id else blocker for blocker, blocked in rows}


def list_blocked(db: Session, blocker_id: uuid.UUID) -> list[User]:
    return list(
        db.execute(
            select(User).join(Block, Block.blocked_id == User.id).where(Block.blocker_id == blocker_id)
        ).scalars().all()
    )
