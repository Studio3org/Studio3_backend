"""/api/wallet: what Apple Wallet and Google Wallet call, and the email's download link.

Apple's pass web service paths and status codes are Apple's contract
(developer.apple.com, "Wallet Passes web service"), so these answer with bare statuses and
bodies rather than the API's usual envelope. Authorisation is the pass's own token
(`Authorization: ApplePass <token>`), never a user login: the caller is the iPhone's Wallet.
"""
import uuid

from flask import Blueprint, Response, abort, jsonify, request, send_from_directory

from src.shared.config.database import SessionLocal
from src.shared.models.ticketing import TicketOrder
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.wallet import apple_pass, wallet_service

logger = get_logger(__name__)
wallet_bp = Blueprint("wallet", __name__)


def _apple_error(exc: AppError):
    return Response(status=exc.status_code)


@wallet_bp.get("/assets/<path:name>")
def asset(name):
    """The logo Google Wallet shows on a pass (it fetches images by URL)."""
    if name not in apple_pass.IMAGE_FILES:
        abort(404)
    return send_from_directory(apple_pass.ASSETS, name, mimetype="image/png", max_age=86400)


@wallet_bp.get("/apple/orders/<order_id>.pkpass")
def apple_download(order_id):
    """The ticket email's "Add to Apple Wallet": a signed link, so no login is needed."""
    if not wallet_service.check_download_signature(order_id, request.args.get("exp"),
                                                   request.args.get("sig")):
        abort(403)
    db = SessionLocal()
    try:
        try:
            order = db.get(TicketOrder, uuid.UUID(order_id))
        except ValueError:
            abort(404)
        result = wallet_service.order_passes(db, order, platform="apple")
    except AppError as exc:
        return Response(exc.message, status=exc.status_code)
    finally:
        db.close()
    return Response(result["body"], mimetype=result["mimetype"], headers={
        "Content-Disposition": f'attachment; filename="{result["filename"]}"'})


# --- Apple's pass web service (v1) ------------------------------------------------------------

@wallet_bp.post("/apple/v1/devices/<device_id>/registrations/<pass_type_id>/<serial>")
def apple_register(device_id, pass_type_id, serial):
    body = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        status = wallet_service.register_device(
            db, device_id, pass_type_id, serial, body.get("pushToken") or "",
            request.headers.get("Authorization", ""))
        return Response(status=status)
    except AppError as exc:
        return _apple_error(exc)
    finally:
        db.close()


@wallet_bp.delete("/apple/v1/devices/<device_id>/registrations/<pass_type_id>/<serial>")
def apple_unregister(device_id, pass_type_id, serial):
    db = SessionLocal()
    try:
        wallet_service.unregister_device(db, device_id, pass_type_id, serial,
                                         request.headers.get("Authorization", ""))
        return Response(status=200)
    except AppError as exc:
        return _apple_error(exc)
    finally:
        db.close()


@wallet_bp.get("/apple/v1/devices/<device_id>/registrations/<pass_type_id>")
def apple_updated(device_id, pass_type_id):
    db = SessionLocal()
    try:
        result = wallet_service.updated_serials(db, device_id, pass_type_id,
                                                request.args.get("passesUpdatedSince"))
        return jsonify(result) if result else Response(status=204)
    finally:
        db.close()


@wallet_bp.get("/apple/v1/passes/<pass_type_id>/<serial>")
def apple_latest(pass_type_id, serial):
    db = SessionLocal()
    try:
        body, updated_at = wallet_service.latest_pass(db, pass_type_id, serial,
                                                      request.headers.get("Authorization", ""))
    except AppError as exc:
        return _apple_error(exc)
    finally:
        db.close()
    since = request.if_modified_since
    if since is not None and updated_at.replace(microsecond=0) <= since:
        return Response(status=304)
    response = Response(body, mimetype=apple_pass.PKPASS_MIME)
    response.last_modified = updated_at
    return response


@wallet_bp.post("/apple/v1/log")
def apple_log():
    """Apple reports problems with our passes here; we log them."""
    for line in (request.get_json(silent=True) or {}).get("logs", [])[:50]:
        logger.warning("Apple Wallet: %s", str(line)[:500])
    return Response(status=200)
