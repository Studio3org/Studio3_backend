"""Phase 5: co-host and featured-artist invites, artist approval of tagged pieces, the host's
bidding end, Exhibited records, and the event's time zone from its location (ticketing spec
"Lineup", "Art at events", "Time"; BACKEND_CHANGES B26, B28)."""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.modules.bids import auction_dao
from src.modules.events import host_payouts
from src.shared.models.event import PieceExhibition
from src.shared.models.notification import Notification
from src.shared.models.piece import Piece
from tests.factories import make_event, make_piece, make_user


def _now():
    return datetime.now(timezone.utc)


def _detail(client, event, auth=None):
    return client.get(f"/api/events/{event.id}", headers=auth or {})


def _notes(db, user, type_):
    return db.execute(select(Notification).where(Notification.user_id == user.id,
                                                 Notification.type == type_)).scalars().all()


def _invite(client, auth, event, **people):
    response = client.put(f"/api/events/{event.id}/people", json=people, headers=auth)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


# --- invites -------------------------------------------------------------------------------------

def test_an_invited_artist_shows_only_once_they_accept(db, client, auth_headers):
    host, artist = make_user(db, seller=True), make_user(db, name="Mira Sol")
    event = make_event(db, host)  # a draft
    data = _invite(client, auth_headers(host), event, artistUsernames=[artist.username])

    assert data["artists"] == []
    assert [(i["username"], i["status"], i["role"]) for i in data["invites"]] == [
        (artist.username, "invited", "artist")]
    assert len(_notes(db, artist, "event_invite")) == 1

    # The invitee can open the draft to see what they're saying yes to.
    seen = _detail(client, event, auth_headers(artist))
    assert seen.status_code == 200
    assert seen.get_json()["data"]["viewerInvite"] == {"roles": ["artist"], "status": "invited"}
    pending = client.get("/api/user/me/event-invites", headers=auth_headers(artist)).get_json()["data"]
    assert [i["event"]["id"] for i in pending["invites"]] == [str(event.id)]
    assert _detail(client, event, auth_headers(make_user(db))).status_code == 404

    accepted = client.post(f"/api/events/{event.id}/invite/accept", headers=auth_headers(artist))
    assert accepted.status_code == 200
    assert [a["username"] for a in accepted.get_json()["data"]["artists"]] == [artist.username]
    assert len(_notes(db, host, "event_invite_response")) == 1


def test_a_declined_invite_never_shows_and_an_uninvited_person_cannot_answer(db, client, auth_headers):
    host, artist, stranger = make_user(db, seller=True), make_user(db), make_user(db)
    event = make_event(db, host, status="published")
    _invite(client, auth_headers(host), event, artistUsernames=[artist.username])

    assert client.post(f"/api/events/{event.id}/invite/decline",
                       headers=auth_headers(artist)).status_code == 200
    assert client.post(f"/api/events/{event.id}/invite/accept",
                       headers=auth_headers(stranger)).status_code == 404
    data = _detail(client, event, auth_headers(host)).get_json()["data"]
    assert data["artists"] == []
    assert data["invites"][0]["status"] == "declined"


def test_only_an_accepted_cohost_can_edit_and_scan(db, client, auth_headers):
    host, cohost, buyer = make_user(db, seller=True), make_user(db), make_user(db)
    event = make_event(db, host, status="published")
    _invite(client, auth_headers(host), event, cohostUsernames=[cohost.username])
    rename = {"title": "Renamed by the co-host"}

    assert client.patch(f"/api/events/{event.id}", json=rename,
                        headers=auth_headers(cohost)).status_code == 404, "not until they accept"
    client.post(f"/api/events/{event.id}/invite/accept", headers=auth_headers(cohost))

    assert client.patch(f"/api/events/{event.id}", json=rename,
                        headers=auth_headers(cohost)).status_code == 200
    created = client.post(f"/api/events/{event.id}/ticket-types", headers=auth_headers(cohost),
                          json={"name": "Door", "priceCents": 0, "quantity": 5})
    assert created.status_code == 201
    client.post(f"/api/events/{event.id}/orders", headers=auth_headers(buyer),
                json={"items": [{"ticketTypeId": created.get_json()["data"]["id"], "quantity": 1}]})
    code = client.get(f"/api/user/me/tickets?eventId={event.id}",
                      headers=auth_headers(buyer)).get_json()["data"]["tickets"][0]["code"]
    scan = client.post(f"/api/events/{event.id}/tickets/{code}/check-in", headers=auth_headers(cohost))
    assert scan.get_json()["data"]["result"] == "checked_in"
    # Cancelling and changing the bill stay the host's.
    assert client.post(f"/api/events/{event.id}/cancel", headers=auth_headers(cohost)).status_code == 404


# --- piece approval ------------------------------------------------------------------------------

def test_a_tagged_piece_waits_for_its_artist_then_lists_at_their_price(db, client, auth_headers):
    host, artist = make_user(db, seller=True), make_user(db, seller=True)
    piece = make_piece(db, artist, price_cents=300_00, status="live")
    event = make_event(db, host, status="published")

    added = client.post(f"/api/events/{event.id}/pieces", headers=auth_headers(host),
                        json={"pieceId": str(piece.id), "mode": "sale", "priceCents": 250_00})
    assert added.get_json()["data"]["approvalStatus"] == "waiting"
    assert _detail(client, event).get_json()["data"]["lineup"] == [], "hidden until approved"
    host_view = _detail(client, event, auth_headers(host)).get_json()["data"]["lineup"]
    assert host_view[0]["approvalStatus"] == "waiting"
    request = _notes(db, artist, "event_piece_request")[0]
    assert request.payload["endsFixedListing"] is True and request.payload["priceCents"] == 250_00
    mine = client.get("/api/user/me/piece-requests", headers=auth_headers(artist)).get_json()["data"]
    assert [r["pieceId"] for r in mine["requests"]] == [str(piece.id)]

    approved = client.post(f"/api/events/{event.id}/pieces/{piece.id}/approve",
                           json={"priceCents": 280_00}, headers=auth_headers(artist))
    assert approved.status_code == 200, approved.get_json()
    assert approved.get_json()["data"]["approvalStatus"] == "approved"
    assert approved.get_json()["data"]["listingEnded"] is True

    db.expire_all()
    piece = db.get(Piece, piece.id)
    assert piece.price_cents == 280_00 and piece.status == "live", "listed for the event"
    public = _detail(client, event).get_json()["data"]["lineup"]
    assert [p["pieceId"] for p in public] == [str(piece.id)]
    assert len(_notes(db, host, "event_piece_response")) == 1


def test_a_declined_piece_stays_off_and_only_its_artist_can_answer(db, client, auth_headers):
    host, artist, other = make_user(db, seller=True), make_user(db, seller=True), make_user(db)
    piece = make_piece(db, artist, price_cents=300_00, status="live")
    event = make_event(db, host)
    client.post(f"/api/events/{event.id}/pieces", headers=auth_headers(host),
                json={"pieceId": str(piece.id), "mode": "featured"})

    assert client.post(f"/api/events/{event.id}/pieces/{piece.id}/approve",
                       headers=auth_headers(other)).status_code == 404
    assert client.post(f"/api/events/{event.id}/pieces/{piece.id}/decline",
                       headers=auth_headers(artist)).status_code == 200
    assert client.post(f"/api/events/{event.id}/pieces/{piece.id}/approve",
                       headers=auth_headers(artist)).status_code == 409
    db.expire_all()
    assert db.get(Piece, piece.id).status == "live"


def test_the_hosts_own_pieces_are_approved_and_an_event_never_mixes_sale_and_auction(
    db, client, auth_headers
):
    host = make_user(db, seller=True)
    first, second = (make_piece(db, host, price_cents=200_00, status="live") for _ in range(2))
    event = make_event(db, host)
    added = client.post(f"/api/events/{event.id}/pieces", headers=auth_headers(host),
                        json={"pieceId": str(first.id), "mode": "sale", "priceCents": 200_00})
    assert added.get_json()["data"]["mode"] == "sale" and "approvalStatus" in added.get_json()["data"]
    assert added.get_json()["data"]["approvalStatus"] == "approved"

    mixed = client.post(f"/api/events/{event.id}/pieces", headers=auth_headers(host),
                        json={"pieceId": str(second.id), "mode": "bid", "priceCents": 200_00})
    assert mixed.status_code == 409 and "not both" in mixed.get_json()["message"]


# --- the bidding end -------------------------------------------------------------------------------

def test_the_host_chooses_when_bidding_ends_at_the_latest_30_minutes_before(db, client, auth_headers):
    host = make_user(db, seller=True)
    piece = make_piece(db, host, price_cents=200_00, status="live")
    event = make_event(db, host, duration=timedelta(hours=4))
    auth = auth_headers(host)

    too_late = client.patch(f"/api/events/{event.id}", headers=auth,
                            json={"auctionEndsAt": (event.ends_at - timedelta(minutes=10)).isoformat()})
    assert too_late.status_code == 400
    chosen = event.ends_at - timedelta(hours=1)
    ok = client.patch(f"/api/events/{event.id}", headers=auth, json={"auctionEndsAt": chosen.isoformat()})
    assert ok.status_code == 200 and ok.get_json()["data"]["auctionEndsAt"] == chosen.isoformat()

    client.post(f"/api/events/{event.id}/pieces", headers=auth,
                json={"pieceId": str(piece.id), "mode": "bid", "priceCents": 200_00})
    assert client.post(f"/api/events/{event.id}/publish", headers=auth).status_code == 200
    auction = auction_dao.get_running_auction(db, piece.id)
    assert auction.closes_at == chosen

    # A reschedule keeps the same distance from the end.
    new_start = event.starts_at + timedelta(days=2)
    moved = client.post(f"/api/events/{event.id}/reschedule", headers=auth, json={
        "startsAt": new_start.isoformat(), "endsAt": (new_start + timedelta(hours=4)).isoformat()})
    assert moved.status_code == 200, moved.get_json()
    db.expire_all()
    assert auction_dao.get_running_auction(db, piece.id).closes_at == new_start + timedelta(hours=3)


# --- after the event --------------------------------------------------------------------------------

def test_a_finished_event_adds_exhibited_to_each_approved_piece_once(db, client, auth_headers):
    host, artist = make_user(db, seller=True), make_user(db, seller=True)
    mine = make_piece(db, host, price_cents=200_00, status="live")
    theirs = make_piece(db, artist, price_cents=200_00, status="live")
    event = make_event(db, host, status="published", title="Night Market", city="Austin")
    client.post(f"/api/events/{event.id}/pieces", headers=auth_headers(host),
                json={"pieceId": str(mine.id), "mode": "featured"})
    client.post(f"/api/events/{event.id}/pieces", headers=auth_headers(host),
                json={"pieceId": str(theirs.id), "mode": "featured"})  # never approved
    event.starts_at = _now() - timedelta(hours=5)
    event.ends_at = _now() - timedelta(hours=2)
    db.commit()

    host_payouts.archive_and_pay()
    host_payouts.archive_and_pay()

    rows = db.execute(select(PieceExhibition)).scalars().all()
    assert [(r.piece_id, r.title, r.city) for r in rows] == [(mine.id, "Night Market", "Austin")]
    shown = client.get(f"/api/pieces/{mine.id}").get_json()["data"]["exhibitions"]
    assert shown[0]["title"] == "Night Market" and shown[0]["venueName"] == "Cedars Union"
    assert client.get(f"/api/pieces/{theirs.id}").get_json()["data"]["exhibitions"] == []


# --- time zone ------------------------------------------------------------------------------------

def test_the_time_zone_comes_from_the_location_not_the_phone(db, client, auth_headers):
    host = make_user(db, seller=True)
    start = _now() + timedelta(days=5)
    created = client.post("/api/events", headers=auth_headers(host), json={
        "title": "Austin show", "startsAt": start.isoformat(),
        "endsAt": (start + timedelta(hours=3)).isoformat(),
        "timezone": "America/New_York", "latitude": 30.2672, "longitude": -97.7431,
    })
    assert created.status_code == 201, created.get_json()
    assert created.get_json()["data"]["timezone"] == "America/Chicago"

    event_id = created.get_json()["data"]["id"]
    moved = client.patch(f"/api/events/{event_id}", headers=auth_headers(host),
                         json={"latitude": 34.05, "longitude": -118.24})
    assert moved.get_json()["data"]["timezone"] == "America/Los_Angeles"
    no_place = client.post("/api/events", headers=auth_headers(host), json={
        "title": "Online", "startsAt": start.isoformat(),
        "endsAt": (start + timedelta(hours=1)).isoformat(), "timezone": "America/Denver"})
    assert no_place.get_json()["data"]["timezone"] == "America/Denver", "the phone is the fallback"
    assert uuid.UUID(event_id)
