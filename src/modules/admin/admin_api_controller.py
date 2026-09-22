"""Admin console API — the JSON the web app's /admin screens read and act through.

A deliberate second face on the same operations as the server-rendered pages in
`admin_controller.py`. Every one of them delegates to the same DAO and services, so the two
cannot drift on anything that matters: a refund issued here takes exactly the path a refund
issued there does, writes the same ledger entries and the same audit row.

What differs is only the shell. These return JSON and raise `AppError`, letting the global
handler answer; the HTML pages render their errors back into the page because their caller is
a browser that cannot read a JSON body.

Authorization is `admin_required` — the JWT gate, since the web app already holds a bearer
token. The cookie-session gate belongs to the HTML pages and is not used here.
"""
import uuid

from flask import g, request
from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.models import audit
from src.shared.models.piece import Piece
from src.shared.models.post import Post
from src.shared.models.report import REPORT_DISMISSED, REPORT_RESOLVED
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.admin import admin_api_serializers as ser
from src.modules.admin import admin_dao, audit_service, disputes_service
from src.modules.admin.admin_controller import ORDER_STATUSES
from src.modules.orders import orders_dao
from src.modules.payments import payouts_service
from src.modules.reports import report_dao
from src.modules.shipments import shipments_dao

logger = get_logger(__name__)

ORDERS_PER_PAGE = 50
AUDIT_PER_PAGE = 100


def _admin_id() -> uuid.UUID:
    """The signed-in admin. `admin_required` has already proved they are one."""
    return uuid.UUID(g.user["id"])


def _uuid_or_404(raw: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError, AttributeError):
        raise AppError(f"{what} not found.", 404)


def _page() -> int:
    try:
        return max(int(request.args.get("page", 1)), 1)
    except (TypeError, ValueError):
        return 1


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _cents(raw, field: str):
    """Ops types dollars; the system stores cents."""
    if raw in (None, ""):
        return None
    try:
        return int(round(float(raw) * 100))
    except (TypeError, ValueError):
        raise AppError(f"{field} must be a number.", 400)


def _audit(action: str, *, subject_id, subject_type: str = "order",
           detail: dict | None = None, note: str | None = None) -> None:
    """Record an ops action against the signed-in admin.

    Its own session on purpose: the services above manage their own transactions and have
    already committed by the time this runs, so borrowing one would mean either reopening a
    closed session or holding one across a Stripe call.
    """
    db = SessionLocal()
    try:
        audit_service.record(
            db, action,
            actor=db.get(User, _admin_id()),
            subject_type=subject_type,
            subject_id=subject_id,
            detail=detail,
            note=note,
        )
    finally:
        db.close()


# --- what the console needs to render itself -------------------------------------------

def summary():
    """Counts for the navigation, so a queue with work in it is visible without opening it."""
    db = SessionLocal()
    try:
        return {
            "ordersByStatus": admin_dao.count_by_status(db),
            "openDisputes": admin_dao.count_open_disputes(db),
            "failedPayouts": admin_dao.count_failed_payouts(db),
            "auctionsNeedingAttention": admin_dao.count_auctions_needing_attention(db),
            "openReports": report_dao.count_open_reports(db),
            "orderStatuses": list(ORDER_STATUSES),
            "auditActions": list(audit.AUDIT_ACTIONS),
            "couriers": list(shipments_dao.KNOWN_COURIERS),
            "shipmentStatuses": list(shipments_dao.VALID_SHIPMENT_STATUSES),
        }, 200
    finally:
        db.close()


# --- orders ------------------------------------------------------------------------------

def list_orders():
    status = request.args.get("status") or None
    page = _page()
    db = SessionLocal()
    try:
        return {
            "orders": [
                ser.order_row(o)
                for o in admin_dao.list_orders(
                    db, status=status, limit=ORDERS_PER_PAGE,
                    offset=(page - 1) * ORDERS_PER_PAGE,
                )
            ],
            "total": admin_dao.count_orders(db, status=status),
            "page": page,
            "perPage": ORDERS_PER_PAGE,
        }, 200
    finally:
        db.close()


def get_order(order_id: str):
    oid = _uuid_or_404(order_id, "Order")
    db = SessionLocal()
    try:
        detail = admin_dao.get_order_full(db, oid)
        if not detail:
            raise AppError("Order not found.", 404)
        return ser.order_full(detail), 200
    finally:
        db.close()


def create_shipment(order_id: str):
    """Ops recording a booked courier pickup."""
    oid = _uuid_or_404(order_id, "Order")
    body = _body()
    db = SessionLocal()
    try:
        order = orders_dao.get_order_for_update(db, oid)
        if not order:
            raise AppError("Order not found.", 404)
        shipments_dao.create_shipment(
            db, order,
            courier=body.get("courier"),
            tracking_number=body.get("trackingNumber"),
            admin_id=_admin_id(),
            actual_cost_cents=_cents(body.get("actualShippingCost"), "Shipping cost"),
        )
    finally:
        db.close()
    _audit(audit.AUDIT_SHIPMENT_CREATED, subject_id=oid, detail={"courier": body.get("courier")})
    return get_order(order_id)


def update_shipment(order_id: str):
    oid = _uuid_or_404(order_id, "Order")
    body = _body()
    db = SessionLocal()
    try:
        order = orders_dao.get_order_for_update(db, oid)
        if not order:
            raise AppError("Order not found.", 404)
        shipments_dao.update_shipment(
            db, order,
            status=body.get("status") or None,
            # Correctable on purpose. A tracking number is typed by hand off a courier
            # label, and the old console could only ever move the status — so a typo left
            # the collector following a parcel that was not theirs, with no way to fix it.
            courier=(body.get("courier") or "").strip() or None,
            tracking_number=(body.get("trackingNumber") or "").strip() or None,
            actual_cost_cents=_cents(body.get("actualShippingCost"), "Shipping cost"),
        )
    finally:
        db.close()
    _audit(
        audit.AUDIT_SHIPMENT_UPDATED,
        subject_id=oid,
        # What changed, not just that something did: a corrected tracking number is the
        # kind of edit somebody asks about later.
        detail={
            k: v for k, v in {
                "status": body.get("status"),
                "courier": body.get("courier"),
                "trackingNumber": body.get("trackingNumber"),
            }.items() if v
        },
    )
    return get_order(order_id)


def resolve_dispute(order_id: str):
    """Refund the collector, or release the order and pay the artist."""
    oid = _uuid_or_404(order_id, "Order")
    body = _body()
    action = (body.get("action") or "").strip()
    reason = (body.get("reason") or "").strip()

    # The reason is the audit record. Without it nobody can answer, a month later, why
    # somebody's money moved.
    if not reason:
        raise AppError("A resolution reason is required — it's the audit record.", 400)
    if action not in ("refund", "release"):
        raise AppError("Choose refund or release.", 400)

    admin_id = _admin_id()
    if action == "refund":
        disputes_service.refund_order(oid, admin_id, reason)
        recorded, notice = audit.AUDIT_REFUND_ISSUED, "Collector refunded."
    else:
        disputes_service.release_order(oid, admin_id, reason)
        recorded, notice = audit.AUDIT_PAYOUT_RELEASED, "Order completed and payout released."

    # Only after it has committed. Recording an intention that then failed would produce a
    # log of things that did not happen.
    _audit(recorded, subject_id=oid, note=reason)
    detail, _ = get_order(order_id)
    return {"notice": notice, **detail}, 200


def retry_payout(order_id: str):
    oid = _uuid_or_404(order_id, "Order")
    result = payouts_service.retry_payout(oid)
    if result.get("status") != "released":
        raise AppError(result.get("reason") or "Payout could not be released.", 409)
    _audit(audit.AUDIT_PAYOUT_RETRIED, subject_id=oid,
           detail={"amountCents": result.get("amountCents")})
    detail, _ = get_order(order_id)
    return {"notice": "Payout released.", **detail}, 200


# --- queues ------------------------------------------------------------------------------

def list_disputes():
    db = SessionLocal()
    try:
        return {
            "disputes": [
                ser.dispute_row(r)
                for r in admin_dao.list_disputes(db, status=request.args.get("status", "open"))
            ],
            "failedPayouts": [
                ser.failed_payout_row(r) for r in admin_dao.list_failed_payouts(db)
            ],
        }, 200
    finally:
        db.close()


def list_auctions():
    db = SessionLocal()
    try:
        return {
            "needingAttention": [
                ser.auction_row(r) for r in admin_dao.list_auctions_needing_attention(db)
            ],
            "live": [ser.auction_row(r) for r in admin_dao.list_live_auctions(db)],
        }, 200
    finally:
        db.close()


def list_events():
    db = SessionLocal()
    try:
        return {"events": [ser.event_row(r) for r in admin_dao.list_events(db)]}, 200
    finally:
        db.close()


def audit_log():
    db = SessionLocal()
    try:
        action = (request.args.get("action") or "").strip() or None
        page = _page() - 1
        rows = audit_service.recent(
            db, action=action, limit=AUDIT_PER_PAGE, offset=page * AUDIT_PER_PAGE
        )
        return {
            "entries": [
                {
                    "id": str(r.id),
                    "action": r.action,
                    "actorLabel": r.actor_label,
                    "actorType": r.actor_type,
                    "subjectType": r.subject_type,
                    "subjectId": str(r.subject_id) if r.subject_id else None,
                    "detail": r.detail,
                    "note": r.note,
                    "createdAt": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ],
            "page": page + 1,
            "perPage": AUDIT_PER_PAGE,
        }, 200
    finally:
        db.close()


# --- reports -----------------------------------------------------------------------------

def list_reports():
    db = SessionLocal()
    try:
        status = request.args.get("status", "open") or None
        reports = report_dao.list_reports(db, status=status)

        # Resolved in bulk rather than per row: a queue of fifty reports would otherwise be
        # fifty round trips to name fifty targets.
        reporters = _by_id(db, User, {r.reporter_id for r in reports})
        pieces = _by_id(db, Piece, {r.target_id for r in reports if r.target_type == "piece"})
        posts = _by_id(db, Post, {r.target_id for r in reports if r.target_type == "post"})
        targets = _by_id(db, User, {r.target_id for r in reports if r.target_type == "user"})

        def label(r):
            if r.target_type == "piece":
                piece = pieces.get(r.target_id)
                return piece.title if piece else "Deleted piece"
            if r.target_type == "post":
                post = posts.get(r.target_id)
                return (post.caption or "Scene")[:60] if post else "Deleted scene"
            user = targets.get(r.target_id)
            return f"@{user.username}" if user else "Deleted account"

        return {
            "reports": [
                {
                    "id": str(r.id),
                    "targetType": r.target_type,
                    "targetId": str(r.target_id) if r.target_id else None,
                    "targetLabel": label(r),
                    "reason": r.reason,
                    "status": r.status,
                    "reporter": _person(reporters.get(r.reporter_id)),
                    "createdAt": r.created_at.isoformat() if r.created_at else None,
                }
                for r in reports
            ],
            "openCount": report_dao.count_open_reports(db),
        }, 200
    finally:
        db.close()


def _by_id(db, model, ids: set) -> dict:
    if not ids:
        return {}
    return {row.id: row for row in db.execute(select(model).where(model.id.in_(ids))).scalars()}


def _person(user) -> dict | None:
    return None if not user else {"id": str(user.id), "username": user.username, "name": user.name}


def resolve_report(report_id: str):
    rid = _uuid_or_404(report_id, "Report")
    body = _body()
    action = (body.get("action") or "").strip()
    outcomes = {"resolve": REPORT_RESOLVED, "dismiss": REPORT_DISMISSED}
    if action not in outcomes:
        raise AppError("Choose resolve or dismiss.", 400)
    note = (body.get("note") or "").strip()

    db = SessionLocal()
    try:
        report = report_dao.get_report(db, rid)
        if not report:
            raise AppError("Report not found.", 404)
        report_dao.resolve_report(db, report, _admin_id(), outcomes[action], note or None)
    finally:
        db.close()
    _audit(audit.AUDIT_REPORT_RESOLVED, subject_type="report", subject_id=rid,
           detail={"outcome": outcomes[action]}, note=note or None)
    return list_reports()
