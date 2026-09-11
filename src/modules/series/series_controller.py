"""Series controller."""
import uuid

from flask import request, g

from src.shared.config.database import SessionLocal
from src.shared.utils.app_error import AppError
from src.modules.auth.auth_dao import find_user_by_username
from src.modules.user.user_dao import get_user_by_id
from src.modules.pieces.pieces_dao import get_piece
from src.modules.series import series_dao
from src.modules.social import social_dao


def list_for_user(username: str):
    db = SessionLocal()
    try:
        user = find_user_by_username(db, username.lower())
        if not user:
            raise AppError("User not found.", 404)
        viewer_id = uuid.UUID(g.user["id"]) if getattr(g, "user", None) else None
        if not social_dao.can_view_content(db, user, viewer_id):
            raise AppError("This account is private.", 403)
        series_list = series_dao.list_user_series(db, user.id)
        summaries = [series_dao.series_summary_dict(db, s) for s in series_list]
        return [s for s in summaries if s["pieceCount"] > 1], 200
    finally:
        db.close()


def list_for_me(user_id: uuid.UUID):
    db = SessionLocal()
    try:
        series_list = series_dao.list_user_series(db, user_id)
        return [series_dao.series_summary_dict(db, s) for s in series_list], 200
    finally:
        db.close()


def get_detail(series_id: str):
    db = SessionLocal()
    try:
        series = series_dao.get_series(db, uuid.UUID(series_id))
        if not series:
            raise AppError("Series not found.", 404)
        owner = get_user_by_id(db, series.user_id)
        viewer_id = uuid.UUID(g.user["id"]) if getattr(g, "user", None) else None
        if owner and not social_dao.can_view_content(db, owner, viewer_id):
            raise AppError("This account is private.", 403)
        return series_dao.series_detail_dict(db, series), 200
    finally:
        db.close()


def _require_owned_series(db, series_id: uuid.UUID, user_id: uuid.UUID):
    series = series_dao.get_series(db, series_id)
    if not series or series.user_id != user_id:
        raise AppError("Series not found.", 404)
    return series


def create():
    body = request.get_json() or {}
    name = (body.get("name") or "").strip()
    if not name:
        raise AppError("Series name is required.", 400)
    piece_ids = body.get("pieceIds") or []
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        series = series_dao.create_series(db, user.id, name[:200])
        description = (body.get("description") or "").strip()
        if description:
            series.description = description[:4000]
            db.commit()
        for pid in piece_ids:
            piece = get_piece(db, uuid.UUID(pid))
            if not piece or piece.user_id != user.id:
                continue
            existing = series_dao.get_series_for_piece(db, piece.id)
            if existing:
                continue
            series_dao.add_piece_to_series(db, series, piece)
        return series_dao.series_detail_dict(db, series), 201
    finally:
        db.close()


def patch(series_id: str):
    body = request.get_json() or {}
    db = SessionLocal()
    try:
        series = _require_owned_series(db, uuid.UUID(series_id), uuid.UUID(g.user["id"]))
        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name:
                raise AppError("Series name is required.", 400)
            series.name = name[:200]
            db.commit()
        if "description" in body:
            raw = body.get("description")
            text = (raw or "").strip()
            series.description = text[:4000] if text else None
            db.commit()
        if "pieceOrder" in body:
            piece_order = [uuid.UUID(pid) for pid in body["pieceOrder"]]
            series_dao.set_piece_order(db, series.id, piece_order)
        db.refresh(series)
        return series_dao.series_detail_dict(db, series), 200
    finally:
        db.close()


def add_piece(series_id: str):
    body = request.get_json() or {}
    piece_id = body.get("pieceId")
    if not piece_id:
        raise AppError("pieceId is required.", 400)
    db = SessionLocal()
    try:
        series = _require_owned_series(db, uuid.UUID(series_id), uuid.UUID(g.user["id"]))
        piece = get_piece(db, uuid.UUID(piece_id))
        if not piece or piece.user_id != series.user_id:
            raise AppError("Piece not found.", 404)
        existing = series_dao.get_series_for_piece(db, piece.id)
        if existing and existing.id != series.id:
            raise AppError("Piece already belongs to another series.", 400)
        if not existing:
            series_dao.add_piece_to_series(db, series, piece)
        return series_dao.series_detail_dict(db, series), 200
    finally:
        db.close()


def remove_piece(series_id: str, piece_id: str):
    db = SessionLocal()
    try:
        series = _require_owned_series(db, uuid.UUID(series_id), uuid.UUID(g.user["id"]))
        series_dao.remove_piece_from_series(db, series.id, uuid.UUID(piece_id))
        return series_dao.series_detail_dict(db, series), 200
    finally:
        db.close()
