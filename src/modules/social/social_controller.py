"""Social graph controller."""
import uuid
from datetime import datetime

from flask import request, g
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.shared.config.database import SessionLocal
from src.shared.models.social import Follow, Like, Comment, Save
from src.shared.models.user import User
from src.shared.models.piece import Piece
from src.shared.models.post import Post
from src.shared.utils.app_error import AppError
from src.modules.auth.auth_dao import find_user_by_username
from src.modules.user.user_dao import get_user_by_id
from src.modules.social import social_dao
from src.modules.social import block_dao
from src.modules.notifications import notifications_dao


def _get_target(db, target_type: str, target_id):
    model = Piece if target_type == "piece" else Post
    row = db.get(model, target_id)
    if not row or getattr(row, "deleted_at", None) is not None:
        return None
    return row


def _get_target_owner_id(db, target_type: str, target_id):
    row = _get_target(db, target_type, target_id)
    return row.user_id if row else None


def _require_target(db, target_type: str, target_id):
    row = _get_target(db, target_type, target_id)
    if not row:
        raise AppError(f"{target_type.capitalize()} not found.", 404)
    return row


def follow(username: str):
    db = SessionLocal()
    try:
        me = get_user_by_id(db, uuid.UUID(g.user["id"]))
        target = find_user_by_username(db, username.lower())
        if not target:
            raise AppError("User not found.", 404)
        if target.id == me.id:
            raise AppError("Cannot follow yourself.", 400)
        if block_dao.is_blocked_either_way(db, me.id, target.id):
            raise AppError("User not found.", 403)

        existing = social_dao.get_follow(db, me.id, target.id)
        if existing:
            # Idempotent: already following, or already requested — no-op either way.
            return {"following": existing.status == "accepted", "requested": existing.status == "pending"}, 200

        # Private accounts require the owner's approval (Instagram-style); public accounts
        # follow immediately, unchanged from before.
        is_private = target.profile_visibility == "private"
        status = "pending" if is_private else "accepted"
        db.add(Follow(id=uuid.uuid4(), follower_id=me.id, following_id=target.id, status=status))
        db.commit()
        notifications_dao.create_and_push(
            db,
            user_id=target.id,
            type="follow_request" if is_private else "follow",
            actor_id=me.id,
            target_type="user",
            target_id=me.id,
            payload={"followerUsername": me.username, "followerName": me.name},
            title="Follow request" if is_private else "New follower",
            body=f"{me.name} requested to follow you" if is_private else f"{me.name} started following you",
        )
        return {"following": not is_private, "requested": is_private}, 200
    finally:
        db.close()


def unfollow(username: str):
    db = SessionLocal()
    try:
        me = get_user_by_id(db, uuid.UUID(g.user["id"]))
        target = find_user_by_username(db, username.lower())
        if target:
            db.query(Follow).filter_by(follower_id=me.id, following_id=target.id).delete()
            db.commit()
        return {"following": False}, 200
    finally:
        db.close()


def list_follow_requests():
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        requests = social_dao.list_follow_requests(db, me_id)
        requester_ids = {r.follower_id for r in requests}
        requesters = {}
        if requester_ids:
            requesters = {u.id: u for u in db.execute(select(User).where(User.id.in_(requester_ids))).scalars()}
        items = []
        for r in requests:
            requester = requesters.get(r.follower_id)
            if not requester:
                continue
            items.append(
                {
                    "username": requester.username,
                    "name": requester.name,
                    "profilePhotoUrl": requester.image,
                    "requestedAt": r.created_at.isoformat(),
                }
            )
        return items, 200
    finally:
        db.close()


def list_followers(username: str, cursor: str = None, limit: int = 20):
    db = SessionLocal()
    try:
        target = find_user_by_username(db, username.lower())
        if not target:
            raise AppError("User not found.", 404)
        viewer_id = uuid.UUID(g.user["id"]) if getattr(g, "user", None) else None
        if not social_dao.can_view_content(db, target, viewer_id):
            raise AppError("This account is private.", 403)
        before = datetime.fromisoformat(cursor) if cursor else None
        rows = social_dao.list_followers(db, target.id, limit=limit + 1, before=before)
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [social_dao.follow_user_to_dict(user) for _follow, user in rows]
        next_cursor = rows[-1][0].created_at.isoformat() if has_more and rows else None
        return {"items": items, "nextCursor": next_cursor}, 200
    finally:
        db.close()


def list_following(username: str, cursor: str = None, limit: int = 20):
    db = SessionLocal()
    try:
        target = find_user_by_username(db, username.lower())
        if not target:
            raise AppError("User not found.", 404)
        viewer_id = uuid.UUID(g.user["id"]) if getattr(g, "user", None) else None
        if not social_dao.can_view_content(db, target, viewer_id):
            raise AppError("This account is private.", 403)
        before = datetime.fromisoformat(cursor) if cursor else None
        rows = social_dao.list_following(db, target.id, limit=limit + 1, before=before)
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [social_dao.follow_user_to_dict(user) for _follow, user in rows]
        next_cursor = rows[-1][0].created_at.isoformat() if has_more and rows else None
        return {"items": items, "nextCursor": next_cursor}, 200
    finally:
        db.close()


def accept_follow_request(username: str):
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        requester = find_user_by_username(db, username.lower())
        if not requester:
            raise AppError("User not found.", 404)
        follow = social_dao.get_follow(db, requester.id, me_id)
        if not follow or follow.status != "pending":
            raise AppError("No pending follow request from this user.", 404)
        social_dao.accept_follow_request(db, follow)
        notifications_dao.create_and_push(
            db,
            user_id=requester.id,
            type="follow",
            actor_id=me_id,
            target_type="user",
            target_id=me_id,
            payload={"accepted": True},
            title="Follow request accepted",
            body="Your follow request was accepted",
        )
        return {"accepted": True}, 200
    finally:
        db.close()


def decline_follow_request(username: str):
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        requester = find_user_by_username(db, username.lower())
        if not requester:
            raise AppError("User not found.", 404)
        follow = social_dao.get_follow(db, requester.id, me_id)
        if not follow or follow.status != "pending":
            raise AppError("No pending follow request from this user.", 404)
        social_dao.decline_follow_request(db, follow)
        return {"declined": True}, 200
    finally:
        db.close()


def _toggle_like(target_type: str, target_id: str, like: bool):
    db = SessionLocal()
    try:
        me = get_user_by_id(db, uuid.UUID(g.user["id"]))
        tid = uuid.UUID(target_id)
        _require_target(db, target_type, tid)
        q = db.query(Like).filter_by(user_id=me.id, target_type=target_type, target_id=tid)
        if like:
            if not q.first():
                try:
                    db.add(Like(id=uuid.uuid4(), user_id=me.id, target_type=target_type, target_id=tid))
                    db.commit()
                except IntegrityError:
                    # Concurrent duplicate like — treat as already liked.
                    db.rollback()
                else:
                    owner_id = _get_target_owner_id(db, target_type, tid)
                    if owner_id and owner_id != me.id:
                        notifications_dao.create_and_push(
                            db,
                            user_id=owner_id,
                            type="like",
                            actor_id=me.id,
                            target_type=target_type,
                            target_id=tid,
                            payload={"likerUsername": me.username, "likerName": me.name},
                            title="New like",
                            body=f"{me.name} liked your {target_type}",
                        )
            return {
                "liked": True,
                "likeCount": social_dao.count_likes(db, target_type, tid),
            }, 200
        q.delete()
        db.commit()
        return {
            "liked": False,
            "likeCount": social_dao.count_likes(db, target_type, tid),
        }, 200
    finally:
        db.close()


def like_piece(piece_id: str):
    return _toggle_like("piece", piece_id, True)


def unlike_piece(piece_id: str):
    return _toggle_like("piece", piece_id, False)


def like_post(post_id: str):
    return _toggle_like("post", post_id, True)


def unlike_post(post_id: str):
    return _toggle_like("post", post_id, False)


def save_target(target_type: str, target_id: str):
    db = SessionLocal()
    try:
        me = get_user_by_id(db, uuid.UUID(g.user["id"]))
        tid = uuid.UUID(target_id)
        _require_target(db, target_type, tid)
        existing = db.query(Save).filter_by(user_id=me.id, target_type=target_type, target_id=tid).first()
        if existing:
            return {
                "saved": True,
                "saveCount": social_dao.count_saves(db, target_type, tid),
            }, 200
        try:
            db.add(Save(id=uuid.uuid4(), user_id=me.id, target_type=target_type, target_id=tid))
            db.commit()
        except IntegrityError:
            db.rollback()
            return {
                "saved": True,
                "saveCount": social_dao.count_saves(db, target_type, tid),
            }, 200
        owner_id = _get_target_owner_id(db, target_type, tid)
        if owner_id and owner_id != me.id:
            notifications_dao.create_and_push(
                db,
                user_id=owner_id,
                type="save",
                actor_id=me.id,
                target_type=target_type,
                target_id=tid,
                payload={"saverUsername": me.username, "saverName": me.name},
                title="New save",
                body=f"{me.name} saved your {target_type}",
            )
        return {
            "saved": True,
            "saveCount": social_dao.count_saves(db, target_type, tid),
        }, 200
    finally:
        db.close()


def unsave_target(target_type: str, target_id: str):
    db = SessionLocal()
    try:
        me = get_user_by_id(db, uuid.UUID(g.user["id"]))
        tid = uuid.UUID(target_id)
        _require_target(db, target_type, tid)
        db.query(Save).filter_by(
            user_id=me.id, target_type=target_type, target_id=tid
        ).delete()
        db.commit()
        return {
            "saved": False,
            "saveCount": social_dao.count_saves(db, target_type, tid),
        }, 200
    finally:
        db.close()


def add_comment(target_type: str, target_id: str):
    body = request.get_json() or {}
    text = (body.get("body") or "").strip()
    if not text:
        raise AppError("Comment body is required.", 400)
    db = SessionLocal()
    try:
        me = get_user_by_id(db, uuid.UUID(g.user["id"]))
        comment = Comment(
            id=uuid.uuid4(),
            user_id=me.id,
            target_type=target_type,
            target_id=uuid.UUID(target_id),
            body=text[:1000],
        )
        db.add(comment)
        db.commit()
        db.refresh(comment)
        owner_id = _get_target_owner_id(db, target_type, comment.target_id)
        if owner_id and owner_id != me.id:
            notifications_dao.create_and_push(
                db,
                user_id=owner_id,
                type="comment",
                actor_id=me.id,
                target_type=target_type,
                target_id=comment.target_id,
                payload={"commentPreview": text[:140], "commenterUsername": me.username},
                title="New comment",
                body=f"{me.name} commented: {text[:100]}",
            )
        comment_dict = social_dao.comment_to_dict(comment, me)
        comment_dict["targetType"] = target_type
        comment_dict["targetId"] = str(comment.target_id)
        try:
            from src.shared.realtime.socketio_instance import socketio

            socketio.emit(
                "comment:new",
                comment_dict,
                room=f"target:{target_type}:{comment.target_id}",
            )
        except Exception:
            pass
        return {
            "id": str(comment.id),
            "body": comment.body,
            "userId": str(me.id),
            "username": me.username,
            "createdAt": comment.created_at.isoformat(),
        }, 201
    finally:
        db.close()


def get_comments(target_type: str, target_id: str, cursor: str | None, limit: int = 50):
    db = SessionLocal()
    try:
        tid = uuid.UUID(target_id)
        before = datetime.fromisoformat(cursor) if cursor else None
        comments = social_dao.list_comments(db, target_type, tid, limit=limit + 1, before=before)
        has_more = len(comments) > limit
        comments = comments[:limit]
        user_ids = {c.user_id for c in comments}
        users = {}
        if user_ids:
            users = {u.id: u for u in db.execute(select(User).where(User.id.in_(user_ids))).scalars()}
        items = [social_dao.comment_to_dict(c, users.get(c.user_id)) for c in comments]
        next_cursor = comments[-1].created_at.isoformat() if has_more and comments else None
        return {"items": items, "nextCursor": next_cursor}, 200
    finally:
        db.close()


def block_user(username: str):
    db = SessionLocal()
    try:
        me = get_user_by_id(db, uuid.UUID(g.user["id"]))
        target = find_user_by_username(db, username.lower())
        if not target:
            raise AppError("User not found.", 404)
        if target.id == me.id:
            raise AppError("Cannot block yourself.", 400)
        block_dao.block_user(db, me.id, target.id)
        return {"blocked": True}, 200
    finally:
        db.close()


def unblock_user(username: str):
    db = SessionLocal()
    try:
        me = get_user_by_id(db, uuid.UUID(g.user["id"]))
        target = find_user_by_username(db, username.lower())
        if target:
            block_dao.unblock_user(db, me.id, target.id)
        return {"blocked": False}, 200
    finally:
        db.close()


def list_blocked():
    db = SessionLocal()
    try:
        me_id = uuid.UUID(g.user["id"])
        blocked = block_dao.list_blocked(db, me_id)
        return [
            {"username": u.username, "name": u.name, "profilePhotoUrl": u.image} for u in blocked
        ], 200
    finally:
        db.close()
