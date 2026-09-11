"""Pieces DAO and serializers."""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func, delete
from sqlalchemy.orm import Session

from src.shared.models.piece import Piece
from src.shared.models.piece_media import PieceMedia


def create_piece(db: Session, **kwargs) -> Piece:
    piece = Piece(id=uuid.uuid4(), **kwargs)
    db.add(piece)
    db.commit()
    db.refresh(piece)
    return piece


def get_piece(db: Session, piece_id: uuid.UUID) -> Optional[Piece]:
    return db.execute(
        select(Piece).where(Piece.id == piece_id, Piece.deleted_at.is_(None))
    ).scalar_one_or_none()


def list_user_pieces(
    db: Session,
    user_id: uuid.UUID,
    for_sale_only: bool = False,
    include_drafts: bool = False,
) -> list[Piece]:
    q = select(Piece).where(Piece.user_id == user_id, Piece.deleted_at.is_(None))
    if not include_drafts:
        q = q.where(Piece.status != "draft")
    if for_sale_only:
        q = q.where(Piece.is_for_sale == True, Piece.status == "live")
    return list(db.execute(q.order_by(Piece.created_at.desc())).scalars().all())


def count_user_pieces(db: Session, user_id: uuid.UUID, include_drafts: bool = False) -> int:
    q = select(func.count(Piece.id)).where(
        Piece.user_id == user_id,
        Piece.deleted_at.is_(None),
    )
    if not include_drafts:
        q = q.where(Piece.status != "draft")
    return db.execute(q).scalar_one()


def list_saved_pieces(db: Session, user_id: uuid.UUID) -> list[Piece]:
    from src.shared.models.social import Save

    q = (
        select(Piece)
        .join(Save, Save.target_id == Piece.id)
        .where(
            Save.user_id == user_id,
            Save.target_type == "piece",
            Piece.deleted_at.is_(None),
        )
        .order_by(Save.created_at.desc())
    )
    return list(db.execute(q).scalars().all())


def replace_piece_media(db: Session, piece_id: uuid.UUID, images: list[dict]) -> list[PieceMedia]:
    """Replaces a piece's whole image gallery. `images` is ordered — index 0
    becomes the cover (`sort_order` 0), mirrored onto the piece's own
    scalar media fields by the caller."""
    db.execute(delete(PieceMedia).where(PieceMedia.piece_id == piece_id))
    rows = [
        PieceMedia(
            id=uuid.uuid4(),
            piece_id=piece_id,
            media_url=image["mediaUrl"],
            media_type=image.get("mediaType", "image"),
            media_aspect_ratio=image.get("mediaAspectRatio"),
            sort_order=index,
        )
        for index, image in enumerate(images)
    ]
    db.add_all(rows)
    db.commit()
    return rows


def list_piece_media(db: Session, piece_id: uuid.UUID) -> list[PieceMedia]:
    return list(
        db.execute(
            select(PieceMedia)
            .where(PieceMedia.piece_id == piece_id)
            .order_by(PieceMedia.sort_order.asc())
        ).scalars().all()
    )


def batch_list_piece_media(
    db: Session, piece_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[PieceMedia]]:
    """Avoids N+1 queries when serializing a list of pieces."""
    if not piece_ids:
        return {}
    rows = db.execute(
        select(PieceMedia)
        .where(PieceMedia.piece_id.in_(piece_ids))
        .order_by(PieceMedia.piece_id, PieceMedia.sort_order.asc())
    ).scalars().all()
    result: dict[uuid.UUID, list[PieceMedia]] = {}
    for row in rows:
        result.setdefault(row.piece_id, []).append(row)
    return result


def delete_piece(db: Session, piece: Piece) -> None:
    piece.deleted_at = datetime.now(timezone.utc)
    piece.status = "deleted"
    db.commit()


def piece_listing_state(piece: Piece) -> Optional[str]:
    """Marketplace badge for feed cards: available to buy, collected, or none."""
    if piece.status in ("sold", "reserved"):
        return "collected"
    if piece.status == "delisted" and piece.is_for_sale:
        return "collected"
    if piece.is_for_sale and piece.status == "live":
        return "available"
    return None


def piece_to_dict(piece: Piece, media: Optional[list[PieceMedia]] = None) -> dict:
    listing_state = piece_listing_state(piece)
    images = [
        {
            "mediaUrl": m.media_url,
            "mediaType": m.media_type,
            "mediaAspectRatio": m.media_aspect_ratio,
            "sortOrder": m.sort_order,
        }
        for m in (media or [])
    ]
    return {
        "id": str(piece.id),
        "userId": str(piece.user_id),
        "title": piece.title,
        "mediaUrl": piece.media_url,
        "mediaType": piece.media_type,
        # Full ordered gallery (Figma 2716:5774 cover/reorder flow) — index 0
        # is always the cover and matches the scalar mediaUrl/mediaType
        # above, kept for clients that only read a single cover image.
        "images": images,
        "caption": piece.caption,
        "medium": piece.medium,
        "materials": piece.materials or [],
        "styleTags": piece.style_tags or [],
        "aiDisclosed": piece.ai_disclosed,
        "altText": piece.alt_text,
        "isForSale": piece.is_for_sale,
        "listingState": listing_state,
        "priceCents": piece.price_cents,
        "currency": piece.currency,
        "dimensions": piece.dimensions,
        "shippingRegion": piece.shipping_region,
        "weightKg": piece.weight_kg,
        "packageLengthCm": piece.package_length_cm,
        "packageWidthCm": piece.package_width_cm,
        "packageHeightCm": piece.package_height_cm,
        "declaredValueCents": piece.declared_value_cents,
        "location": piece.location,
        "mediaAspectRatio": piece.media_aspect_ratio,
        "yearCreated": piece.year_created,
        "framingMounting": piece.framing_mounting,
        "provenance": piece.provenance,
        "handlingNotes": piece.handling_notes,
        "status": piece.status,
        "createdAt": piece.created_at.isoformat(),
    }
