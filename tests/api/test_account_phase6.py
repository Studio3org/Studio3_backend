"""Phase 6: account and settings (BACKEND_CHANGES B34, B32's hide from Collection)."""
import uuid
from datetime import timedelta

from sqlalchemy import select

from src.shared.models.notification import Notification
from src.shared.models.piece import Piece
from src.shared.models.social import Follow
from tests.factories import make_event, make_order, make_piece, make_user


def _notes(db, user, type_):
    return db.execute(select(Notification).where(Notification.user_id == user.id,
                                                 Notification.type == type_)).scalars().all()


def _follow_request(db, follower, target):
    db.add(Follow(id=uuid.uuid4(), follower_id=follower.id, following_id=target.id, status="pending"))
    db.commit()


# --- going public ----------------------------------------------------------------------------------

def test_going_public_accepts_everyone_waiting_once_and_tells_them(db, client, auth_headers):
    owner = make_user(db, profile_visibility="private")
    a, b = make_user(db), make_user(db)
    _follow_request(db, a, owner)
    _follow_request(db, b, owner)

    response = client.patch("/api/user/me", json={"profileVisibility": "public"},
                            headers=auth_headers(owner))
    assert response.status_code == 200, response.get_json()
    db.expire_all()
    assert {f.status for f in db.query(Follow).filter_by(following_id=owner.id)} == {"accepted"}
    assert len(_notes(db, a, "follow")) == 1 and len(_notes(db, b, "follow")) == 1

    # Saving "public" again approves nobody twice.
    client.patch("/api/user/me", json={"profileVisibility": "public"}, headers=auth_headers(owner))
    assert len(_notes(db, a, "follow")) == 1


# --- turning off selling -------------------------------------------------------------------------

def test_turning_off_selling_takes_listings_down_instead_of_refusing(db, client, auth_headers):
    seller = make_user(db, seller=True)
    listed = make_piece(db, seller, price_cents=200_00, status="live")

    response = client.post("/api/user/me/seller/disable", headers=auth_headers(seller))
    assert response.status_code == 200, response.get_json()
    db.expire_all()
    piece = db.get(Piece, listed.id)
    assert piece.is_for_sale is False and piece.deleted_at is None, "the piece stays, not for sale"


def test_turning_off_selling_is_refused_while_a_sale_is_going_through(db, client, auth_headers):
    seller, buyer = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, seller, price_cents=200_00, status="live")
    make_order(db, buyer=buyer, seller=seller, status="shipped")

    response = client.post("/api/user/me/seller/disable", headers=auth_headers(seller))
    assert response.status_code == 409 and "still going through" in response.get_json()["message"]
    assert db.get(Piece, piece.id).is_for_sale is True


# --- deleting ------------------------------------------------------------------------------------

def test_the_deletion_check_lists_what_blocks_and_the_delete_refuses(db, client, auth_headers):
    from src.modules.auth.auth_controller import _hash_password

    host = make_user(db, seller=True, password=_hash_password("correct-horse-1"))
    buyer = make_user(db)
    event = make_event(db, host, status="published", starts_in=timedelta(days=10))
    ga = client.post(f"/api/events/{event.id}/ticket-types", headers=auth_headers(host),
                     json={"name": "GA", "priceCents": 0, "quantity": 5}).get_json()["data"]["id"]
    client.post(f"/api/events/{event.id}/orders", headers=auth_headers(buyer),
                json={"items": [{"ticketTypeId": ga, "quantity": 1}]})

    check = client.get("/api/user/me/deletion-check", headers=auth_headers(host)).get_json()["data"]
    assert check["canDelete"] is False
    assert [b["kind"] for b in check["blockers"]] == ["hosted_event_tickets"]

    refused = client.delete("/api/user/me", json={"password": "correct-horse-1"},
                            headers=auth_headers(host))
    assert refused.status_code == 409, refused.get_json()
    assert "hosting an event with tickets" in refused.get_json()["message"]

    clear = client.get("/api/user/me/deletion-check", headers=auth_headers(buyer)).get_json()["data"]
    assert clear == {"canDelete": True, "blockers": []}


def test_a_shipping_purchase_blocks_deleting(db, client, auth_headers):
    seller, buyer = make_user(db, seller=True), make_user(db)
    make_order(db, buyer=buyer, seller=seller, status="paid")
    kinds = {b["kind"] for b in client.get("/api/user/me/deletion-check",
                                            headers=auth_headers(buyer)).get_json()["data"]["blockers"]}
    assert kinds == {"shipping_purchase"}
    kinds = {b["kind"] for b in client.get("/api/user/me/deletion-check",
                                            headers=auth_headers(seller)).get_json()["data"]["blockers"]}
    assert kinds == {"shipping_sale"}


# --- hide from Collection (B32) ------------------------------------------------------------------

def test_a_hidden_piece_leaves_the_collection_and_comes_back_on_undo(db, client, auth_headers):
    seller, collector = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, seller, price_cents=100_00, status="sold")
    make_order(db, buyer=collector, seller=seller, piece=piece, status="completed")
    url = f"/api/users/{collector.username}/collection"

    assert [p["id"] for p in client.get(url).get_json()["data"]] == [str(piece.id)]
    hide = client.post(f"/api/collection/{piece.id}/hide", headers=auth_headers(collector))
    assert hide.status_code == 200
    assert client.get(url).get_json()["data"] == []
    assert client.get(url, headers=auth_headers(collector)).get_json()["data"] == []
    owner_all = client.get(f"{url}?includeHidden=1", headers=auth_headers(collector)).get_json()["data"]
    assert owner_all[0]["hidden"] is True

    assert client.delete(f"/api/collection/{piece.id}/hide", headers=auth_headers(collector)).status_code == 200
    assert [p["id"] for p in client.get(url).get_json()["data"]] == [str(piece.id)]
    stranger = make_user(db)
    assert client.post(f"/api/collection/{piece.id}/hide", headers=auth_headers(stranger)).status_code == 404


# --- seller terms ----------------------------------------------------------------------------------

def test_seller_terms_show_the_accounts_own_rate(db, client, auth_headers):
    standard, founding = make_user(db, seller=True), make_user(db, seller=True)
    founding.commission_bps_override = 1500
    db.commit()
    plain = client.get("/api/user/me/seller-terms", headers=auth_headers(standard)).get_json()["data"]
    mine = client.get("/api/user/me/seller-terms", headers=auth_headers(founding)).get_json()["data"]
    assert mine["artCommissionBps"] == 1500 and mine["reducedRate"] is True
    assert plain["artCommissionBps"] != 1500 and plain["reducedRate"] is False
    assert mine["ticketFeeBps"] == 800 and mine["buyersPayFees"] is False
