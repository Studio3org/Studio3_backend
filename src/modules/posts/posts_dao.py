"""Posts DAO."""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.models.post import Post


def create_post(db: Session, **kwargs) -> Post:
    post = Post(id=uuid.uuid4(), **kwargs)
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


def get_post(db: Session, post_id: uuid.UUID) -> Optional[Post]:
    return db.execute(
        select(Post).where(Post.id == post_id, Post.deleted_at.is_(None))
    ).scalar_one_or_none()


def list_user_posts(db: Session, user_id: uuid.UUID) -> list[Post]:
    return list(
        db.execute(
            select(Post)
            .where(Post.user_id == user_id, Post.deleted_at.is_(None), Post.status == "live")
            .order_by(Post.created_at.desc())
        ).scalars().all()
    )


def list_related_posts(db: Session, piece_id: uuid.UUID) -> list[Post]:
    return list(
        db.execute(
            select(Post).where(Post.linked_piece_id == piece_id, Post.deleted_at.is_(None))
        ).scalars().all()
    )


def list_saved_posts(db: Session, user_id: uuid.UUID) -> list[Post]:
    from src.shared.models.social import Save

    q = (
        select(Post)
        .join(Save, Save.target_id == Post.id)
        .where(
            Save.user_id == user_id,
            Save.target_type == "post",
            Post.deleted_at.is_(None),
        )
        .order_by(Save.created_at.desc())
    )
    return list(db.execute(q).scalars().all())


def delete_post(db: Session, post: Post) -> None:
    post.deleted_at = datetime.now(timezone.utc)
    post.status = "deleted"
    db.commit()


def post_to_dict(post: Post) -> dict:
    return {
        "id": str(post.id),
        "userId": str(post.user_id),
        "mediaUrl": post.media_url,
        "mediaType": post.media_type,
        "caption": post.caption,
        "location": post.location,
        "mediaAspectRatio": post.media_aspect_ratio,
        "thumbnailUrl": post.thumbnail_url,
        "isProcess": post.is_process,
        "linkedPieceId": str(post.linked_piece_id) if post.linked_piece_id else None,
        "status": post.status,
        "createdAt": post.created_at.isoformat(),
    }
