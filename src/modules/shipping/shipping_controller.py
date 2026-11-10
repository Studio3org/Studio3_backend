"""Shipping endpoints (Phase 8).

* `POST /api/pieces/:id/shipping-quotes` — a Convelio quote to one of the buyer's addresses.
* `GET  /api/shipping-quotes/:id` — re-read it (a pending custom quote becomes ready).
* `GET  /api/pieces/:id/shipping-estimate?zip=&state=` — the piece page's range (B19).
* `GET|PUT /api/user/me/pickup-address` — the artist's studio pickup address (B31), private.
"""
import uuid

from flask import g, request

from src.modules.shipping import convelio_mapper, quote_service, shipping_config
from src.shared.config.database import SessionLocal
from src.shared.models.address import Address
from src.shared.models.shipping import ShippingQuote
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.modules.pieces.pieces_dao import get_piece


def _uuid(raw, what):
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        raise AppError(f"Invalid {what} id.", 400) from None


def _me(db):
    return db.get(User, uuid.UUID(g.user["id"]))


def create_quote(piece_id: str):
    body = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        buyer = _me(db)
        piece = get_piece(db, _uuid(piece_id, "piece"))
        if piece is None:
            raise AppError("Piece not found.", 404)
        if piece.user_id == buyer.id:
            raise AppError("This is your own piece.", 400)
        address = db.get(Address, _uuid(body.get("addressId"), "address"))
        if address is None or address.user_id != buyer.id:
            raise AppError("Address not found.", 404)
        insurance = body.get("insurance")
        quote = quote_service.request_quote(
            db, piece=piece, buyer=buyer, address=address,
            speed=(body.get("speed") or "regular_speed").strip(),
            delivery_type=(body.get("deliveryType") or "curbside").strip(),
            insurance=None if insurance is None else bool(insurance),
        )
        return quote_service.quote_to_dict(quote), 201
    finally:
        db.close()


def get_quote(quote_id: str):
    db = SessionLocal()
    try:
        quote = db.get(ShippingQuote, _uuid(quote_id, "quote"))
        if quote is None or quote.buyer_id != uuid.UUID(g.user["id"]):
            raise AppError("Shipping quote not found.", 404)
        return quote_service.quote_to_dict(quote), 200
    finally:
        db.close()


def estimate(piece_id: str):
    db = SessionLocal()
    try:
        piece = get_piece(db, _uuid(piece_id, "piece"))
        if piece is None:
            raise AppError("Piece not found.", 404)
        zip_code = (request.args.get("zip") or "").strip()[:10] or None
        state = (request.args.get("state") or "").strip()[:2].upper() or None
        result = quote_service.estimate(db, piece, postcode=zip_code, state=state)
        result["live"] = shipping_config.enabled()
        return result, 200
    finally:
        db.close()


def get_pickup_address():
    db = SessionLocal()
    try:
        me = _me(db)
        address = db.get(Address, me.pickup_address_id) if me.pickup_address_id else None
        return {"pickupAddress": _address(address)}, 200
    finally:
        db.close()


def set_pickup_address():
    """B31: one of the artist's own US addresses, with a phone Convelio can call."""
    body = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        me = _me(db)
        raw = body.get("addressId")
        if raw in (None, ""):
            me.pickup_address_id = None
            db.commit()
            return {"pickupAddress": None}, 200
        address = db.get(Address, _uuid(raw, "address"))
        if address is None or address.user_id != me.id:
            raise AppError("Address not found.", 404)
        if (address.country or "").upper() != "US":
            raise AppError("Pickup addresses are US only for now.", 400)
        if not convelio_mapper.phone_e164(address.phone):
            raise AppError("Add a phone number to this address: the courier calls it to arrange pickup.", 400)
        me.pickup_address_id = address.id
        db.commit()
        return {"pickupAddress": _address(address)}, 200
    finally:
        db.close()


def _address(address):
    if address is None:
        return None
    return {"id": str(address.id), "firstName": address.first_name, "lastName": address.last_name,
            "phone": address.phone, "line1": address.line1, "line2": address.line2,
            "city": address.city, "state": address.state, "zip": address.zip,
            "country": address.country}


def readiness(db, piece) -> dict:
    """For the piece's owner: what's missing before it can ship through Convelio."""
    missing = convelio_mapper.missing_for_piece(piece)
    owner = db.get(User, piece.user_id)
    if not getattr(owner, "pickup_address_id", None):
        missing.append("pickup address")
    return {"ready": not missing, "missing": missing}
