"""Admin UI controller — renders HTML, not JSON.

Errors are rendered back into the page rather than raised as AppError, since the global
error handler returns JSON and the caller here is a browser.
"""
import uuid

import bcrypt
from flask import g, redirect, render_template, request, url_for
from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.models.user import User
from src.shared.models.piece import Piece
from src.shared.models.post import Post
from src.shared.models.report import REPORT_RESOLVED, REPORT_DISMISSED
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.shared.utils.rate_limit import rate_limit_ip
from src.modules.admin import admin_dao, disputes_service
from src.modules.admin.admin_auth import login_session, logout_session
from src.modules.orders import orders_dao
from src.modules.payments import payouts_service
from src.modules.shipments import shipments_dao
from src.modules.reports import report_dao

logger = get_logger(__name__)

ORDER_STATUSES = (
    "pending_payment",
    "paid",
    "shipped",
    "awaiting_confirmation",
    "completed",
    "disputed",
    "refunded",
    "cancelled",
    "failed",
)


def login_form():
    return render_template("admin/login.html", error=None)


def login():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    try:
        # Admin accounts can move money — throttle harder than the consumer login.
        rate_limit_ip("admin_login", limit=5, window_seconds=300)
    except AppError as e:
        return render_template("admin/login.html", error=e.message), 429

    if not email or not password:
        return render_template("admin/login.html", error="Email and password are required."), 400

    db = SessionLocal()
    try:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        # One generic message for every failure mode (unknown email, wrong password, not an
        # admin) so this page can't be used to enumerate accounts or discover who is staff.
        invalid = "Invalid credentials."
        if not user or not user.password or not user.is_admin:
            logger.warning("Admin login rejected for %s", email)
            return render_template("admin/login.html", error=invalid), 401
        if not bcrypt.checkpw(password.encode(), user.password.encode()):
            logger.warning("Admin login failed (bad password) for %s", email)
            return render_template("admin/login.html", error=invalid), 401

        login_session(user)
        logger.info("Admin login succeeded for %s", email)
        return redirect(url_for("admin.orders_list"))
    finally:
        db.close()


def logout():
    logout_session()
    return redirect(url_for("admin.login_form"))


def orders_list():
    status = request.args.get("status") or None
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    per_page = 50

    db = SessionLocal()
    try:
        orders = admin_dao.list_orders(db, status=status, limit=per_page, offset=(page - 1) * per_page)
        total = admin_dao.count_orders(db, status=status)
        return render_template(
            "admin/orders_list.html",
            admin=g.admin,
            orders=orders,
            counts=admin_dao.count_by_status(db),
            statuses=ORDER_STATUSES,
            active_status=status,
            page=page,
            per_page=per_page,
            total=total,
        )
    finally:
        db.close()


def order_detail(order_id: str, error: str = None, notice: str = None):
    db = SessionLocal()
    try:
        oid = _parse_uuid(order_id)
        if not oid:
            return render_template("admin/not_found.html", admin=g.admin), 404
        detail = admin_dao.get_order_full(db, oid)
        if not detail:
            return render_template("admin/not_found.html", admin=g.admin), 404
        return render_template(
            "admin/order_detail.html",
            admin=g.admin,
            error=error,
            notice=notice,
            couriers=shipments_dao.KNOWN_COURIERS,
            shipment_statuses=shipments_dao.VALID_SHIPMENT_STATUSES,
            **detail,
        )
    finally:
        db.close()


def _parse_uuid(raw: str):
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError, AttributeError):
        return None


def disputes_queue():
    db = SessionLocal()
    try:
        status = request.args.get("status", "open") or None
        return render_template(
            "admin/disputes.html",
            admin=g.admin,
            rows=admin_dao.list_disputes(db, status=status),
            failed_payouts=admin_dao.list_failed_payouts(db),
            open_count=admin_dao.count_open_disputes(db),
            failed_count=admin_dao.count_failed_payouts(db),
            active_status=status,
        )
    finally:
        db.close()


def reports_queue():
    db = SessionLocal()
    try:
        status = request.args.get("status", "open") or None
        reports = report_dao.list_reports(db, status=status)

        reporter_ids = {r.reporter_id for r in reports}
        reporters = (
            {u.id: u for u in db.execute(select(User).where(User.id.in_(reporter_ids))).scalars()}
            if reporter_ids
            else {}
        )
        piece_ids = {r.target_id for r in reports if r.target_type == "piece"}
        post_ids = {r.target_id for r in reports if r.target_type == "post"}
        user_ids = {r.target_id for r in reports if r.target_type == "user"}
        pieces = (
            {p.id: p.title for p in db.execute(select(Piece).where(Piece.id.in_(piece_ids))).scalars()}
            if piece_ids
            else {}
        )
        posts = (
            {
                p.id: (p.caption or "Scene")[:60]
                for p in db.execute(select(Post).where(Post.id.in_(post_ids))).scalars()
            }
            if post_ids
            else {}
        )
        target_users = (
            {u.id: f"@{u.username}" for u in db.execute(select(User).where(User.id.in_(user_ids))).scalars()}
            if user_ids
            else {}
        )

        def target_label(r):
            if r.target_type == "piece":
                return pieces.get(r.target_id, "Deleted piece")
            if r.target_type == "post":
                return posts.get(r.target_id, "Deleted scene")
            return target_users.get(r.target_id, "Deleted account")

        rows = [
            {"report": r, "reporter": reporters.get(r.reporter_id), "target_label": target_label(r)}
            for r in reports
        ]
        return render_template(
            "admin/reports.html",
            admin=g.admin,
            rows=rows,
            open_count=report_dao.count_open_reports(db),
            active_status=status,
            error=request.args.get("error"),
            notice=request.args.get("notice"),
        )
    finally:
        db.close()


def resolve_report(report_id: str):
    rid = _parse_uuid(report_id)
    if not rid:
        return redirect(url_for("admin.reports_queue"))
    action = (request.form.get("action") or "").strip()
    note = (request.form.get("note") or "").strip()
    status_map = {"resolve": REPORT_RESOLVED, "dismiss": REPORT_DISMISSED}
    if action not in status_map:
        return redirect(url_for("admin.reports_queue", error="Choose resolve or dismiss."))

    db = SessionLocal()
    try:
        report = report_dao.get_report(db, rid)
        if not report:
            return redirect(url_for("admin.reports_queue", error="Report not found."))
        report_dao.resolve_report(db, report, g.admin.id, status_map[action], note or None)
    finally:
        db.close()
    return redirect(url_for("admin.reports_queue", notice="Report resolved."))


def create_shipment(order_id: str):
    """Ops recording a booked courier pickup."""
    oid = _parse_uuid(order_id)
    if not oid:
        return redirect(url_for("admin.orders_list"))
    db = SessionLocal()
    try:
        order = orders_dao.get_order_for_update(db, oid)
        if not order:
            return render_template("admin/not_found.html", admin=g.admin), 404
        shipments_dao.create_shipment(
            db,
            order,
            courier=request.form.get("courier"),
            tracking_number=request.form.get("trackingNumber"),
            admin_id=g.admin.id,
            actual_cost_cents=_form_cents(request.form.get("actualShippingCost")),
        )
    except AppError as e:
        return order_detail(order_id, error=e.message)
    finally:
        db.close()
    return redirect(url_for("admin.order_detail", order_id=order_id))


def update_shipment(order_id: str):
    oid = _parse_uuid(order_id)
    if not oid:
        return redirect(url_for("admin.orders_list"))
    db = SessionLocal()
    try:
        order = orders_dao.get_order_for_update(db, oid)
        if not order:
            return render_template("admin/not_found.html", admin=g.admin), 404
        shipments_dao.update_shipment(
            db,
            order,
            status=request.form.get("status") or None,
            actual_cost_cents=_form_cents(request.form.get("actualShippingCost")),
        )
    except AppError as e:
        return order_detail(order_id, error=e.message)
    finally:
        db.close()
    return redirect(url_for("admin.order_detail", order_id=order_id))


def _form_cents(raw):
    """Ops types dollars; the system stores cents."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(round(float(raw) * 100))
    except ValueError:
        raise AppError("Shipping cost must be a number.", 400)


def resolve_dispute(order_id: str):
    oid = _parse_uuid(order_id)
    if not oid:
        return redirect(url_for("admin.disputes_queue"))
    action = (request.form.get("action") or "").strip()
    reason = (request.form.get("reason") or "").strip()

    if not reason:
        return order_detail(order_id, error="A resolution reason is required — it's the audit record.")
    if action not in ("refund", "release"):
        return order_detail(order_id, error="Choose refund or release.")

    try:
        if action == "refund":
            disputes_service.refund_order(oid, g.admin.id, reason)
            notice = "Collector refunded."
        else:
            disputes_service.release_order(oid, g.admin.id, reason)
            notice = "Order completed and payout released."
    except AppError as e:
        return order_detail(order_id, error=e.message)
    return order_detail(order_id, notice=notice)


def retry_payout(order_id: str):
    oid = _parse_uuid(order_id)
    if not oid:
        return redirect(url_for("admin.orders_list"))
    try:
        result = payouts_service.retry_payout(oid)
    except AppError as e:
        return order_detail(order_id, error=e.message)
    if result.get("status") == "released":
        return order_detail(order_id, notice="Payout released.")
    return order_detail(order_id, error=result.get("reason") or "Payout could not be released.")
