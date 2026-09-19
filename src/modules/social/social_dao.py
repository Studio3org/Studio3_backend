"""Social graph DAO — batched lookups for likes/saves/comments/follows."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from src.shared.models.social import Follow, Like, Save, Comment
from src.shared.models.user import User


def count_likes(db: Session, target_type: str, target_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(Like.id)).where(Like.target_type == target_type, Like.target_id == target_id)
    ).scalar_one()


def count_saves(db: Session, target_type: str, target_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(Save.id)).where(Save.target_type == target_type, Save.target_id == target_id)
    ).scalar_one()


def count_comments(db: Session, target_type: str, target_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(Comment.id)).where(Comment.target_type == target_type, Comment.target_id == target_id)
    ).scalar_one()


def user_liked(db: Session, target_type: str, target_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return db.execute(
        select(Like.id).where(
            Like.target_type == target_type, Like.target_id == target_id, Like.user_id == user_id
        )
    ).first() is not None


def user_saved(db: Session, target_type: str, target_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return db.execute(
        select(Save.id).where(
            Save.target_type == target_type, Save.target_id == target_id, Save.user_id == user_id
        )
    ).first() is not None


def user_follows(db: Session, follower_id: Optional[uuid.UUID], following_id: uuid.UUID) -> bool:
    """True only for an ACCEPTED follow — a pending (unapproved) request doesn't count as
    following yet. See has_pending_request() for the "Requested" button state."""
    if not follower_id:
        return False
    return db.execute(
        select(Follow.id).where(
            Follow.follower_id == follower_id,
            Follow.following_id == following_id,
            Follow.status == "accepted",
        )
    ).first() is not None


def has_pending_request(db: Session, follower_id: Optional[uuid.UUID], following_id: uuid.UUID) -> bool:
    if not follower_id:
        return False
    return db.execute(
        select(Follow.id).where(
            Follow.follower_id == follower_id,
            Follow.following_id == following_id,
            Follow.status == "pending",
        )
    ).first() is not None


def count_followers(db: Session, user_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(Follow.id)).where(Follow.following_id == user_id, Follow.status == "accepted")
    ).scalar_one()


def count_following(db: Session, user_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(Follow.id)).where(Follow.follower_id == user_id, Follow.status == "accepted")
    ).scalar_one()


def list_followers(
    db: Session, user_id: uuid.UUID, limit: int = 20, before: Optional[datetime] = None
) -> list[tuple[Follow, User]]:
    """Accepted followers of `user_id` (Instagram-style followers list), newest first."""
    q = (
        select(Follow, User)
        .join(User, User.id == Follow.follower_id)
        .where(Follow.following_id == user_id, Follow.status == "accepted")
    )
    if before:
        q = q.where(Follow.created_at < before)
    rows = db.execute(q.order_by(Follow.created_at.desc()).limit(limit)).all()
    return [(row[0], row[1]) for row in rows]


def list_following(
    db: Session, user_id: uuid.UUID, limit: int = 20, before: Optional[datetime] = None
) -> list[tuple[Follow, User]]:
    """Accounts `user_id` accepted-follows (Instagram-style following list), newest first."""
    q = (
        select(Follow, User)
        .join(User, User.id == Follow.following_id)
        .where(Follow.follower_id == user_id, Follow.status == "accepted")
    )
    if before:
        q = q.where(Follow.created_at < before)
    rows = db.execute(q.order_by(Follow.created_at.desc()).limit(limit)).all()
    return [(row[0], row[1]) for row in rows]


def follow_user_to_dict(user: User) -> dict:
    return {"username": user.username, "name": user.name, "profilePhotoUrl": user.image}


def count_user_saves(db: Session, user_id: uuid.UUID) -> int:
    """Number of pieces/posts this user has saved."""
    return db.execute(select(func.count(Save.id)).where(Save.user_id == user_id)).scalar_one()


def batch_like_counts(db: Session, target_type: str, target_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not target_ids:
        return {}
    rows = db.execute(
        select(Like.target_id, func.count(Like.id))
        .where(Like.target_type == target_type, Like.target_id.in_(target_ids))
        .group_by(Like.target_id)
    ).all()
    return {tid: c for tid, c in rows}


def batch_save_counts(db: Session, target_type: str, target_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not target_ids:
        return {}
    rows = db.execute(
        select(Save.target_id, func.count(Save.id))
        .where(Save.target_type == target_type, Save.target_id.in_(target_ids))
        .group_by(Save.target_id)
    ).all()
    return {tid: c for tid, c in rows}


def batch_comment_counts(db: Session, target_type: str, target_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not target_ids:
        return {}
    rows = db.execute(
        select(Comment.target_id, func.count(Comment.id))
        .where(Comment.target_type == target_type, Comment.target_id.in_(target_ids))
        .group_by(Comment.target_id)
    ).all()
    return {tid: c for tid, c in rows}


def batch_user_likes(db: Session, target_type: str, target_ids: list[uuid.UUID], user_id: Optional[uuid.UUID]) -> set:
    if not user_id or not target_ids:
        return set()
    rows = db.execute(
        select(Like.target_id).where(
            Like.target_type == target_type, Like.target_id.in_(target_ids), Like.user_id == user_id
        )
    ).scalars().all()
    return set(rows)


def batch_user_saves(db: Session, target_type: str, target_ids: list[uuid.UUID], user_id: Optional[uuid.UUID]) -> set:
    if not user_id or not target_ids:
        return set()
    rows = db.execute(
        select(Save.target_id).where(
            Save.target_type == target_type, Save.target_id.in_(target_ids), Save.user_id == user_id
        )
    ).scalars().all()
    return set(rows)


def batch_accepted_following(
    db: Session, follower_id: Optional[uuid.UUID], following_ids: list[uuid.UUID]
) -> set:
    """Set of user ids among `following_ids` that `follower_id` accepted-follows."""
    if not follower_id or not following_ids:
        return set()
    rows = db.execute(
        select(Follow.following_id).where(
            Follow.follower_id == follower_id,
            Follow.following_id.in_(following_ids),
            Follow.status == "accepted",
        )
    ).scalars().all()
    return set(rows)


def count_saves_for_targets(db: Session, target_type: str, target_ids: list[uuid.UUID]) -> int:
    if not target_ids:
        return 0
    return db.execute(
        select(func.count(Save.id)).where(Save.target_type == target_type, Save.target_id.in_(target_ids))
    ).scalar_one()


def count_likes_for_targets(db: Session, target_type: str, target_ids: list[uuid.UUID]) -> int:
    if not target_ids:
        return 0
    return db.execute(
        select(func.count(Like.id)).where(Like.target_type == target_type, Like.target_id.in_(target_ids))
    ).scalar_one()


def list_comments(
    db: Session, target_type: str, target_id: uuid.UUID, limit: int = 50, before: Optional[datetime] = None
) -> list[Comment]:
    q = select(Comment).where(Comment.target_type == target_type, Comment.target_id == target_id)
    if before:
        q = q.where(Comment.created_at < before)
    return list(db.execute(q.order_by(Comment.created_at.desc()).limit(limit)).scalars().all())


def comment_to_dict(comment: Comment, author: Optional[User]) -> dict:
    return {
        "id": str(comment.id),
        "body": comment.body,
        "author": (
            {"username": author.username, "name": author.name, "profilePhotoUrl": author.image}
            if author
            else None
        ),
        "createdAt": comment.created_at.isoformat(),
    }


def get_follow(db: Session, follower_id: uuid.UUID, following_id: uuid.UUID) -> Optional[Follow]:
    return db.execute(
        select(Follow).where(Follow.follower_id == follower_id, Follow.following_id == following_id)
    ).scalar_one_or_none()


def list_follow_requests(db: Session, user_id: uuid.UUID) -> list[Follow]:
    """Pending requests to follow `user_id` (i.e. `user_id` is the account being followed)."""
    return list(
        db.execute(
            select(Follow)
            .where(Follow.following_id == user_id, Follow.status == "pending")
            .order_by(Follow.created_at.desc())
        ).scalars().all()
    )


def accept_follow_request(db: Session, follow: Follow) -> None:
    follow.status = "accepted"
    db.commit()


def decline_follow_request(db: Session, follow: Follow) -> None:
    db.delete(follow)
    db.commit()


def can_view_content(db: Session, profile_user: User, viewer_id: Optional[uuid.UUID]) -> bool:
    """Whether `viewer_id` can see profile_user's pieces/posts/series grids — public accounts
    are open to everyone; private accounts require the owner or an accepted follower."""
    if profile_user.profile_visibility != "private":
        return True
    if viewer_id and viewer_id == profile_user.id:
        return True
    return user_follows(db, viewer_id, profile_user.id)
