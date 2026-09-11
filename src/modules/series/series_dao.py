"""Series DAO."""
import uuid
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from src.shared.models.series import Series, SeriesPiece
from src.shared.models.piece import Piece


def create_series(db: Session, user_id: uuid.UUID, name: str) -> Series:
    series = Series(id=uuid.uuid4(), user_id=user_id, name=name)
    db.add(series)
    db.commit()
    db.refresh(series)
    return series


def get_series(db: Session, series_id: uuid.UUID) -> Optional[Series]:
    return db.get(Series, series_id)


def list_user_series(db: Session, user_id: uuid.UUID) -> list[Series]:
    return list(
        db.execute(
            select(Series).where(Series.user_id == user_id).order_by(Series.created_at.desc())
        ).scalars().all()
    )


def get_series_for_piece(db: Session, piece_id: uuid.UUID) -> Optional[Series]:
    row = db.execute(
        select(Series)
        .join(SeriesPiece, SeriesPiece.series_id == Series.id)
        .where(SeriesPiece.piece_id == piece_id)
    ).scalar_one_or_none()
    return row


def get_series_piece(db: Session, series_id: uuid.UUID, piece_id: uuid.UUID) -> Optional[SeriesPiece]:
    return db.execute(
        select(SeriesPiece).where(SeriesPiece.series_id == series_id, SeriesPiece.piece_id == piece_id)
    ).scalar_one_or_none()


def add_piece_to_series(db: Session, series: Series, piece: Piece, position: Optional[int] = None) -> SeriesPiece:
    if position is None:
        position = count_series_pieces(db, series.id)
    sp = SeriesPiece(id=uuid.uuid4(), series_id=series.id, piece_id=piece.id, position=position)
    db.add(sp)
    db.commit()
    db.refresh(sp)
    return sp


def remove_piece_from_series(db: Session, series_id: uuid.UUID, piece_id: uuid.UUID) -> None:
    db.query(SeriesPiece).filter_by(series_id=series_id, piece_id=piece_id).delete()
    db.commit()


def list_series_pieces(db: Session, series_id: uuid.UUID) -> list[Piece]:
    return list(
        db.execute(
            select(Piece)
            .join(SeriesPiece, SeriesPiece.piece_id == Piece.id)
            .where(SeriesPiece.series_id == series_id, Piece.deleted_at.is_(None))
            .order_by(SeriesPiece.position.asc())
        ).scalars().all()
    )


def count_series_pieces(db: Session, series_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(SeriesPiece.id)).where(SeriesPiece.series_id == series_id)
    ).scalar_one()


def set_piece_order(db: Session, series_id: uuid.UUID, piece_order: list[uuid.UUID]) -> None:
    for position, piece_id in enumerate(piece_order):
        sp = get_series_piece(db, series_id, piece_id)
        if sp:
            sp.position = position
    db.commit()


def series_summary_dict(db: Session, series: Series) -> dict:
    pieces = list_series_pieces(db, series.id)
    newest = sorted(pieces, key=lambda p: p.created_at, reverse=True)
    cover = newest[0].media_url if newest else None
    return {
        "id": str(series.id),
        "name": series.name,
        "description": series.description,
        "pieceCount": len(pieces),
        "coverUrl": cover,
        "previewPieces": [
            {"id": str(p.id), "mediaUrl": p.media_url, "title": p.title}
            for p in newest[:4]
        ],
    }


def series_detail_dict(db: Session, series: Series) -> dict:
    from src.modules.pieces.pieces_dao import piece_to_dict
    from src.shared.models.user import User

    pieces = list_series_pieces(db, series.id)
    newest = sorted(pieces, key=lambda p: p.created_at, reverse=True)
    d = series_summary_dict(db, series)
    d["pieceIds"] = [str(p.id) for p in pieces]
    d["pieces"] = [piece_to_dict(p) for p in newest]
    author = db.get(User, series.user_id)
    if author:
        d["author"] = {
            "username": author.username,
            "name": author.name,
            "profilePhotoUrl": author.image,
        }
    return d
