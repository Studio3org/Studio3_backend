"""The admin console API — the JSON the web app's /admin screens read and act through.

This surface issues refunds and releases artist payouts, so the tests that matter most are
the ones about who may reach it at all.
"""
import uuid

from src.shared.models.audit import AuditEvent
from src.shared.models.order import Order
from tests.factories import make_order, make_piece, make_user

ENDPOINTS = (
    "/api/admin/summary",
    "/api/admin/orders",
    "/api/admin/disputes",
    "/api/admin/auctions",
    "/api/admin/events",
    "/api/admin/audit",
    "/api/admin/reports",
)


def _data(response) -> dict:
    return response.get_json()["data"]


def _order(db, **kwargs):
    seller, buyer = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, seller, price_cents=300_00, status="sold")
    return make_order(db, buyer=buyer, seller=seller, piece=piece,
                      artwork_cents=300_00, **kwargs)


# --- who may reach it ----------------------------------------------------------------------

def test_every_endpoint_refuses_an_ordinary_signed_in_user(db, client, auth_headers):
    """The whole point of the gate. A collector with a valid token is still not staff."""
    collector = make_user(db)
    headers = auth_headers(collector)

    for path in ENDPOINTS:
        assert client.get(path, headers=headers).status_code == 403, path


def test_every_endpoint_refuses_an_unauthenticated_caller(db, client):
    for path in ENDPOINTS:
        assert client.get(path, headers={}).status_code == 401, path


def test_an_admin_reaches_all_of_them(db, client, auth_headers):
    headers = auth_headers(make_user(db, is_admin=True))

    for path in ENDPOINTS:
        assert client.get(path, headers=headers).status_code == 200, path


def test_seller_status_grants_nothing(db, client, auth_headers):
    """Authorization is the is_admin flag alone. role and seller_enabled are product
    categories and must never open this door."""
    seller = make_user(db, seller=True)

    assert client.get("/api/admin/orders", headers=auth_headers(seller)).status_code == 403


def test_revoking_admin_takes_effect_on_the_next_request(db, client, auth_headers):
    """Checked against the database per request, not trusted from the token — otherwise a
    revoked admin keeps their access until the token happens to expire."""
    admin = make_user(db, is_admin=True)
    headers = auth_headers(admin)
    assert client.get("/api/admin/orders", headers=headers).status_code == 200

    admin.is_admin = False
    db.commit()

    assert client.get("/api/admin/orders", headers=headers).status_code == 403


# --- reading -------------------------------------------------------------------------------

def test_the_summary_carries_the_counts_the_navigation_needs(db, client, auth_headers):
    headers = auth_headers(make_user(db, is_admin=True))

    body = _data(client.get("/api/admin/summary", headers=headers))

    for key in ("ordersByStatus", "openDisputes", "failedPayouts",
                "auctionsNeedingAttention", "openReports"):
        assert key in body, key
    # The console renders its own filters from these rather than hardcoding a second copy.
    assert "pending_payment" in body["orderStatuses"]
    assert body["auditActions"] and body["couriers"] and body["shipmentStatuses"]


def test_orders_can_be_filtered_by_status(db, client, auth_headers):
    headers = auth_headers(make_user(db, is_admin=True))
    _order(db, status="paid")
    _order(db, status="refunded")

    body = _data(client.get("/api/admin/orders?status=refunded", headers=headers))

    assert body["total"] == 1
    assert all(o["status"] == "refunded" for o in body["orders"])


def test_an_order_that_does_not_exist_is_a_404_not_a_crash(db, client, auth_headers):
    headers = auth_headers(make_user(db, is_admin=True))

    assert client.get(f"/api/admin/orders/{uuid.uuid4()}", headers=headers).status_code == 404
    # A malformed id is the same answer: it tells an unauthorised prodder nothing.
    assert client.get("/api/admin/orders/not-a-uuid", headers=headers).status_code == 404


# --- acting --------------------------------------------------------------------------------

def test_resolving_a_dispute_requires_a_reason(db, client, auth_headers):
    """The reason is the audit record. Without it nobody can answer, a month later, why
    somebody's money moved."""
    headers = auth_headers(make_user(db, is_admin=True))
    order = _order(db, status="disputed")

    response = client.post(f"/api/admin/orders/{order.id}/resolve",
                           json={"action": "refund", "reason": "  "}, headers=headers)

    assert response.status_code == 400
    db.expire_all()
    assert db.get(Order, order.id).status == "disputed", "nothing may move without a reason"


def test_an_unknown_resolution_is_refused(db, client, auth_headers):
    headers = auth_headers(make_user(db, is_admin=True))
    order = _order(db, status="disputed")

    response = client.post(f"/api/admin/orders/{order.id}/resolve",
                           json={"action": "delete", "reason": "because"}, headers=headers)

    assert response.status_code == 400


def test_a_shipment_cost_that_is_not_a_number_is_refused(db, client, auth_headers):
    headers = auth_headers(make_user(db, is_admin=True))
    order = _order(db, status="paid")

    response = client.post(f"/api/admin/orders/{order.id}/shipment",
                           json={"courier": "fedex", "trackingNumber": "1Z",
                                 "actualShippingCost": "twelve"},
                           headers=headers)

    assert response.status_code == 400


def test_recording_a_shipment_is_written_to_the_audit_log(db, client, auth_headers):
    """Ops actions have to be answerable for months later, when the application log is gone."""
    admin = make_user(db, is_admin=True)
    order = _order(db, status="paid")

    response = client.post(f"/api/admin/orders/{order.id}/shipment",
                           json={"courier": "fedex", "trackingNumber": "1Z999"},
                           headers=auth_headers(admin))

    assert response.status_code == 200
    row = db.query(AuditEvent).filter_by(subject_id=order.id,
                                         action="shipment_created").one()
    assert row.actor_id == admin.id


def test_a_mistyped_tracking_number_can_be_corrected(db, client, auth_headers):
    """Typed by hand off a courier label. Before this the console could only move the
    status, so a typo left the collector following somebody else's parcel."""
    headers = auth_headers(make_user(db, is_admin=True))
    order = _order(db, status="paid")
    client.post(f"/api/admin/orders/{order.id}/shipment",
                json={"courier": "fedex", "trackingNumber": "1Z-WRONG"}, headers=headers)

    response = client.post(f"/api/admin/orders/{order.id}/shipment/update",
                           json={"trackingNumber": "1Z-RIGHT"}, headers=headers)

    assert response.status_code == 200
    assert _data(response)["shipment"]["trackingNumber"] == "1Z-RIGHT"


def test_correcting_one_field_leaves_the_others_alone(db, client, auth_headers):
    """Blank means unchanged. Otherwise fixing a tracking number would clear the courier."""
    headers = auth_headers(make_user(db, is_admin=True))
    order = _order(db, status="paid")
    client.post(f"/api/admin/orders/{order.id}/shipment",
                json={"courier": "fedex", "trackingNumber": "1Z999"}, headers=headers)

    body = _data(client.post(f"/api/admin/orders/{order.id}/shipment/update",
                             json={"status": "in_transit"}, headers=headers))

    assert body["shipment"]["courier"] == "fedex"
    assert body["shipment"]["trackingNumber"] == "1Z999"
    assert body["shipment"]["status"] == "in_transit"


def test_a_tracking_correction_records_what_changed(db, client, auth_headers):
    """"Shipment updated" on its own does not answer the question somebody asks a week
    later, which is what it was updated to."""
    admin = make_user(db, is_admin=True)
    headers = auth_headers(admin)
    order = _order(db, status="paid")
    client.post(f"/api/admin/orders/{order.id}/shipment",
                json={"courier": "fedex", "trackingNumber": "1Z-WRONG"}, headers=headers)

    client.post(f"/api/admin/orders/{order.id}/shipment/update",
                json={"trackingNumber": "1Z-RIGHT"}, headers=headers)

    row = (db.query(AuditEvent)
             .filter_by(subject_id=order.id, action="shipment_updated")
             .one())
    assert row.detail["trackingNumber"] == "1Z-RIGHT"


def test_resolving_a_report_needs_a_real_outcome(db, client, auth_headers):
    headers = auth_headers(make_user(db, is_admin=True))

    response = client.post(f"/api/admin/reports/{uuid.uuid4()}/resolve",
                           json={"action": "maybe"}, headers=headers)

    assert response.status_code == 400


def test_the_audit_log_can_be_filtered_and_comes_back_newest_first(db, client, auth_headers):
    admin = make_user(db, is_admin=True)
    headers = auth_headers(admin)
    order = _order(db, status="paid")
    client.post(f"/api/admin/orders/{order.id}/shipment",
                json={"courier": "fedex", "trackingNumber": "1Z999"}, headers=headers)

    body = _data(client.get("/api/admin/audit?action=shipment_created", headers=headers))

    assert body["entries"], "the action just taken should be in its own filter"
    assert all(e["action"] == "shipment_created" for e in body["entries"])
    assert body["entries"][0]["actorLabel"]


# --- what the client uses to decide whether to show the console ----------------------------

def test_a_user_is_told_whether_they_are_an_admin(db, client, auth_headers):
    admin = make_user(db, is_admin=True)

    body = client.get("/api/user/me", headers=auth_headers(admin)).get_json()["data"]

    assert body["isAdmin"] is True


def test_an_ordinary_user_is_told_they_are_not(db, client, auth_headers):
    body = client.get("/api/user/me", headers=auth_headers(make_user(db))).get_json()["data"]

    assert body.get("isAdmin") is False
