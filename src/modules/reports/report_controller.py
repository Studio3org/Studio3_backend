"""Reports controller — flagging a piece, post, or user for moderation review."""
import uuid

from flask import g, request
from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.models.report import Report, REPORT_REASONS
from src.shared.models.piece import Piece
from src.shared.models.post import Post
from src.shared.models.user import User
from src.shared.models.social import Comment
from src.shared.utils.app_error import AppError
from src.modules.auth.auth_dao import find_user_by_username
from src.modules.user.user_dao import get_user_by_id
from src.modules.reports import report_dao
from src.modules.notifications import notifications_dao


def _validate_reason(body: dict) -> tuple[str, str | None]:
    reason = (body.get("reason") or "").strip().lower()
    if reason not in REPORT_REASONS:
        raise AppError("Choose a valid reason for this report.", 400)
    details = (body.get("details") or "").strip() or None
    if details and len(details) > 1000:
        details = details[:1000]
    return reason, details


def _report_to_dict(report: Report, target_label: str | None = None) -> dict:
    return {
        "id": str(report.id),
        "targetType": report.target_type,
        "targetId": str(report.target_id),
        "targetLabel": target_label,
        "reason": report.reason,
        "status": report.status,
        "createdAt": report.created_at.isoformat(),
    }


def notify_admins(db, actor_id, target_type, target_id, payload, title, body):
    """Best-effort push to every admin, so moderation hears about it without polling."""
    try:
        for admin in db.execute(select(User).where(User.is_admin.is_(True))).scalars():
            notifications_dao.create_and_push(
                db,
                user_id=admin.id,
                type="system",
                actor_id=actor_id,
                target_type=target_type,
                target_id=target_id,
                payload=payload,
                title=title,
                body=body,
            )
    except Exception:
        pass


def _create_or_reuse(db, reporter_id, target_type, target_id, reason, details):
    existing = report_dao.find_open_report(db, reporter_id, target_type, target_id)
    if existing:
        # Idempotent: don't let repeated taps pile up duplicate open reports.
        return existing
    report = report_dao.create_report(db, reporter_id, target_type, target_id, reason, details)
    notify_admins(
        db,
        reporter_id,
        target_type,
        target_id,
        payload={"reason": reason},
        title="New report",
        body=f"A {target_type} was reported for {reason.replace('_', ' ')}",
    )
    return report


def report_piece(piece_id: str):
    body = request.get_json() or {}
    reason, details = _validate_reason(body)
    db = SessionLocal()
    try:
        reporter_id = uuid.UUID(g.user["id"])
        tid = uuid.UUID(piece_id)
        piece = db.get(Piece, tid)
        if not piece or getattr(piece, "deleted_at", None) is not None:
            raise AppError("Piece not found.", 404)
        report = _create_or_reuse(db, reporter_id, "piece", tid, reason, details)
        return _report_to_dict(report, target_label=piece.title), 200
    finally:
        db.close()


def report_post(post_id: str):
    body = request.get_json() or {}
    reason, details = _validate_reason(body)
    db = SessionLocal()
    try:
        reporter_id = uuid.UUID(g.user["id"])
        tid = uuid.UUID(post_id)
        post = db.get(Post, tid)
        if not post or getattr(post, "deleted_at", None) is not None:
            raise AppError("Post not found.", 404)
        report = _create_or_reuse(db, reporter_id, "post", tid, reason, details)
        label = (post.caption or "Scene")[:60]
        return _report_to_dict(report, target_label=label), 200
    finally:
        db.close()


def report_comment(comment_id: str):
    body = request.get_json() or {}
    reason, details = _validate_reason(body)
    db = SessionLocal()
    try:
        reporter_id = uuid.UUID(g.user["id"])
        try:
            tid = uuid.UUID(comment_id)
        except ValueError:
            raise AppError("Comment not found.", 404)
        comment = db.get(Comment, tid)
        if not comment:
            raise AppError("Comment not found.", 404)
        if comment.user_id == reporter_id:
            raise AppError("Cannot report your own comment.", 400)
        report = _create_or_reuse(db, reporter_id, "comment", tid, reason, details)
        return _report_to_dict(report, target_label=comment_label(comment)), 200
    finally:
        db.close()


def comment_label(comment: Comment | None) -> str:
    if not comment:
        return "Deleted comment"
    return f"“{comment.body[:60]}”"


def report_user(username: str):
    body = request.get_json() or {}
    reason, details = _validate_reason(body)
    db = SessionLocal()
    try:
        me = get_user_by_id(db, uuid.UUID(g.user["id"]))
        target = find_user_by_username(db, username.lower())
        if not target:
            raise AppError("User not found.", 404)
        if target.id == me.id:
            raise AppError("Cannot report yourself.", 400)
        report = _create_or_reuse(db, me.id, "user", target.id, reason, details)
        return _report_to_dict(report, target_label=f"@{target.username}"), 200
    finally:
        db.close()


def list_my_reports():
    db = SessionLocal()
    try:
        reporter_id = uuid.UUID(g.user["id"])
        reports = report_dao.list_my_reports(db, reporter_id)

        piece_ids = {r.target_id for r in reports if r.target_type == "piece"}
        post_ids = {r.target_id for r in reports if r.target_type == "post"}
        user_ids = {r.target_id for r in reports if r.target_type == "user"}
        comment_ids = {r.target_id for r in reports if r.target_type == "comment"}

        comments = {}
        if comment_ids:
            comments = {
                c.id: c
                for c in db.execute(select(Comment).where(Comment.id.in_(comment_ids))).scalars()
            }
        pieces = {}
        if piece_ids:
            pieces = {
                p.id: p.title
                for p in db.execute(select(Piece).where(Piece.id.in_(piece_ids))).scalars()
            }
        posts = {}
        if post_ids:
            posts = {
                p.id: (p.caption or "Scene")[:60]
                for p in db.execute(select(Post).where(Post.id.in_(post_ids))).scalars()
            }
        users = {}
        if user_ids:
            users = {
                u.id: f"@{u.username}"
                for u in db.execute(select(User).where(User.id.in_(user_ids))).scalars()
            }

        def label_for(r: Report):
            if r.target_type == "piece":
                return pieces.get(r.target_id, "Deleted piece")
            if r.target_type == "post":
                return posts.get(r.target_id, "Deleted scene")
            if r.target_type == "comment":
                return comment_label(comments.get(r.target_id))
            return users.get(r.target_id, "Deleted account")

        return [_report_to_dict(r, target_label=label_for(r)) for r in reports], 200
    finally:
        db.close()
