"""Phase 2A over HTTP: log out of other devices (B34), the piece on order rows (B34), and the
admin retry for a refund that failed at Stripe."""
from datetime import datetime, timezone

import bcrypt
import pytest

from src.modules.admin import disputes_service
from src.modules.admin.admin_auth import SESSION_STARTED_KEY, SESSION_USER_KEY
from src.modules.payments import money
from src.shared.models.order import Order
from src.shared.utils.app_error import AppError
from tests.factories import make_order, make_payout, make_piece, make_user


def _with_password(db, user, password="correct-horse-1"):
    user.password = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    db.commit()
    return password


def _login(client, user, password):
    response = client.post("/api/auth/login", json={"username": user.email, "password": password})
    assert response.status_code == 200, response.get_json()
    return {"Authorization": f"Bearer {response.get_json()['data']['accessToken']}"}


def test_log_out_everywhere_keeps_this_device(db, client, flush_redis):
    user = make_user(db)
    password = _with_password(db, user)
    this_phone = _login(client, user, password)
    other_phone = _login(client, user, password)

    response = client.post("/api/auth/logout-all", headers=this_phone)

    assert response.status_code == 200
    assert client.get("/api/user/me", headers=this_phone).status_code == 200
    assert client.get("/api/user/me", headers=other_phone).status_code == 401


def test_order_rows_carry_their_piece(db, client, auth_headers):
    seller, buyer = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, seller, title="Paper Planes III", status="sold")
    make_order(db, buyer=buyer, seller=seller, piece=piece, status="paid")

    purchases = client.get("/api/user/me/orders", headers=auth_headers(buyer)).get_json()["data"]
    sales = client.get("/api/user/me/sales", headers=auth_headers(seller)).get_json()["data"]

    for rows in (purchases["items"], sales["items"]):
        item = rows[0]["items"][0]
        assert item["piece"]["id"] == str(piece.id)
        assert item["piece"]["title"] == "Paper Planes III"
        assert item["piece"]["mediaUrl"] == piece.media_url
        assert rows[0]["fromAuction"] is False


def test_an_admin_retries_a_refund_that_failed(db, client, stripe_enabled):
    seller, buyer = make_user(db, seller=True), make_user(db)
    order = make_order(
        db, buyer=buyer, seller=seller, piece=make_piece(db, seller, status="sold"),
        status="paid", payment_reference="pi_retry_1", stripe_charge_id="ch_retry_1",
    )
    money.book_order_paid(db, order, stripe_fee_cents=320)
    make_payout(db, order)
    admin = make_user(db, is_admin=True)
    stripe_enabled.refund_error = RuntimeError("Stripe timed out")
    with pytest.raises(AppError):
        disputes_service.refund_order(order.id, admin.id, "not as described")

    with client.session_transaction() as session:
        session[SESSION_USER_KEY] = str(admin.id)
        session[SESSION_STARTED_KEY] = datetime.now(timezone.utc).timestamp()

    page = client.get("/admin/disputes").get_data(as_text=True)
    assert "Refunds that didn&#39;t go through (1)" in page or "Refunds that didn't go through (1)" in page

    assert client.post(f"/admin/orders/{order.id}/retry-refund").status_code == 200
    db.expire_all()
    assert db.get(Order, order.id).status == "refunded"
    assert len(stripe_enabled.refunds) == 1
