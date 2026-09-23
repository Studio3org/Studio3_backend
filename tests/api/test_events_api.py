"""Events over HTTP: the host's create flow, and what a visitor can see.

Entry is free and open, so browse and detail work signed out. Everything that writes needs an
account, because publishing an event puts real work on sale.
"""
from datetime import datetime, timedelta, timezone

from src.shared.models.event import Event
from tests.factories import make_event, make_piece, make_user


def _window(days_out=7, hours=3):
    starts = datetime.now(timezone.utc) + timedelta(days=days_out)
    return {
        "startsAt": starts.isoformat(),
        "endsAt": (starts + timedelta(hours=hours)).isoformat(),
    }


def _create(client, headers, **overrides):
    body = {"title": "Collector's Preview", "category": "gallery_walk", **_window(), **overrides}
    return client.post("/api/events", json=body, headers=headers)


# --- create and edit -------------------------------------------------------------------------

def test_an_event_is_created_as_a_draft(db, client, auth_headers):
    """Publishing is what puts work on sale, so it is a separate, deliberate step — a host
    has to be able to build a bill without anything going live under them."""
    host = make_user(db, seller=True)

    response = _create(client, auth_headers(host))

    assert response.status_code == 201, response.get_json()
    body = response.get_json()["data"]
    assert body["status"] == "draft"
    assert body["isHost"] is True
    assert body["isFree"] is True


def test_an_event_must_end_after_it_starts(db, client, auth_headers):
    host = make_user(db, seller=True)
    starts = datetime.now(timezone.utc) + timedelta(days=3)

    response = _create(
        client, auth_headers(host),
        startsAt=starts.isoformat(),
        endsAt=(starts - timedelta(hours=1)).isoformat(),
    )

    assert response.status_code == 400


def test_an_unknown_category_is_refused(db, client, auth_headers):
    host = make_user(db, seller=True)

    response = _create(client, auth_headers(host), category="banana")

    assert response.status_code == 400


def test_only_the_host_can_edit(db, client, auth_headers):
    host, stranger = make_user(db, seller=True), make_user(db)
    event = make_event(db, host)

    response = client.patch(
        f"/api/events/{event.id}", json={"title": "Hijacked"}, headers=auth_headers(stranger)
    )

    # 404, not 403: whether an id is a real draft is not a stranger's to probe for.
    assert response.status_code == 404


def test_a_published_events_date_cannot_be_moved(db, client, auth_headers):
    """Its auctions already take their window from these times and have bids against them."""
    host = make_user(db, seller=True)
    event = make_event(db, host, status="published")

    response = client.patch(
        f"/api/events/{event.id}", json=_window(days_out=14), headers=auth_headers(host)
    )

    assert response.status_code == 409


# --- publishing -------------------------------------------------------------------------------

def test_publishing_puts_the_event_out_and_lists_its_bill(db, client, auth_headers):
    host = make_user(db, seller=True)
    piece = make_piece(db, host, price_cents=300_00, status="draft")
    event = make_event(db, host)
    client.post(
        f"/api/events/{event.id}/pieces",
        json={"pieceId": str(piece.id), "mode": "bid", "priceCents": 400_00},
        headers=auth_headers(host),
    )

    response = client.post(f"/api/events/{event.id}/publish", headers=auth_headers(host))

    assert response.status_code == 200, response.get_json()
    body = response.get_json()["data"]
    assert body["status"] == "published"
    assert body["auctionListings"] == 1


def test_publishing_an_event_that_has_already_ended_is_refused(db, client, auth_headers):
    host = make_user(db, seller=True)
    now = datetime.now(timezone.utc)
    event = make_event(
        db, host,
        starts_at=now - timedelta(days=2), ends_at=now - timedelta(days=1),
    )

    response = client.post(f"/api/events/{event.id}/publish", headers=auth_headers(host))

    assert response.status_code == 409


def test_publishing_is_idempotent(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = make_event(db, host)

    first = client.post(f"/api/events/{event.id}/publish", headers=auth_headers(host))
    second = client.post(f"/api/events/{event.id}/publish", headers=auth_headers(host))

    assert first.status_code == 200
    assert second.status_code == 200
    db.expire_all()
    assert db.get(Event, event.id).status == "published"


def test_cancelling_reports_what_it_took_down(db, client, auth_headers):
    host = make_user(db, seller=True)
    piece = make_piece(db, host, price_cents=300_00, status="draft")
    event = make_event(db, host)
    client.post(
        f"/api/events/{event.id}/pieces",
        json={"pieceId": str(piece.id), "mode": "bid", "priceCents": 400_00},
        headers=auth_headers(host),
    )
    client.post(f"/api/events/{event.id}/publish", headers=auth_headers(host))

    response = client.post(
        f"/api/events/{event.id}/cancel", json={"reason": "Venue flooded"},
        headers=auth_headers(host),
    )

    assert response.status_code == 200
    body = response.get_json()["data"]
    assert body["status"] == "cancelled"
    assert body["cancelledAuctions"] == 1
    assert body["cancellationReason"] == "Venue flooded"


def test_deleting_a_draft_just_removes_it(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = make_event(db, host)

    response = client.delete(f"/api/events/{event.id}", headers=auth_headers(host))

    assert response.status_code == 200
    assert response.get_json()["data"]["deleted"] is True
    db.expire_all()
    assert db.get(Event, event.id) is None


def test_deleting_a_published_event_winds_it_down_first(db, client, auth_headers):
    host = make_user(db, seller=True)
    piece = make_piece(db, host, price_cents=300_00, status="draft")
    event = make_event(db, host)
    client.post(
        f"/api/events/{event.id}/pieces",
        json={"pieceId": str(piece.id), "mode": "bid", "priceCents": 400_00},
        headers=auth_headers(host),
    )
    client.post(f"/api/events/{event.id}/publish", headers=auth_headers(host))

    response = client.delete(f"/api/events/{event.id}", headers=auth_headers(host))

    assert response.status_code == 200
    assert response.get_json()["data"]["deleted"] is True
    db.expire_all()
    # The row is gone, same as a draft — but the piece it was auctioning came down with it
    # rather than being left listed under an event that no longer exists.
    assert db.get(Event, event.id) is None
    db.refresh(piece)
    assert piece.status == "delisted"


def test_a_stranger_cannot_delete_someone_elses_event(db, client, auth_headers):
    host, stranger = make_user(db, seller=True), make_user(db, seller=True)
    event = make_event(db, host)

    response = client.delete(f"/api/events/{event.id}", headers=auth_headers(stranger))

    assert response.status_code == 404
    db.expire_all()
    assert db.get(Event, event.id) is not None


# --- the bill ---------------------------------------------------------------------------------

def test_the_tagging_preview_says_what_would_be_ended(db, client, auth_headers):
    from tests.factories import make_auction, make_bid

    host = make_user(db, seller=True)
    bidder = make_user(db)
    piece = make_piece(db, host, price_cents=300_00, status="live", listing_type="auction")
    auction = make_auction(db, piece, host, starting_bid_cents=300_00)
    make_bid(db, auction, bidder, 350_00)
    event = make_event(db, host)

    response = client.get(
        f"/api/events/{event.id}/pieces/{piece.id}/tagging-preview",
        headers=auth_headers(host),
    )

    assert response.status_code == 200
    body = response.get_json()["data"]
    assert body["endsAuction"] is True
    assert body["activeBidCount"] == 1
    assert body["ownedByViewer"] is True


def test_a_host_cannot_sell_another_artists_work_over_http(db, client, auth_headers):
    host, artist = make_user(db, seller=True), make_user(db, seller=True)
    piece = make_piece(db, artist, price_cents=300_00, status="live")
    event = make_event(db, host)

    response = client.post(
        f"/api/events/{event.id}/pieces",
        json={"pieceId": str(piece.id), "mode": "sale", "priceCents": 300_00},
        headers=auth_headers(host),
    )

    assert response.status_code == 403


def test_a_billed_artist_can_add_their_own_work(db, client, auth_headers):
    from tests.factories import make_event_participant

    host, artist = make_user(db, seller=True), make_user(db, seller=True)
    piece = make_piece(db, artist, price_cents=300_00, status="live")
    event = make_event(db, host)
    make_event_participant(db, event, artist, role="artist")

    response = client.post(
        f"/api/events/{event.id}/pieces",
        json={"pieceId": str(piece.id), "mode": "sale", "priceCents": 320_00},
        headers=auth_headers(artist),
    )

    assert response.status_code == 201, response.get_json()
    assert response.get_json()["data"]["mode"] == "sale"


def test_someone_with_no_connection_to_the_event_cannot_touch_the_bill(db, client, auth_headers):
    host, stranger = make_user(db, seller=True), make_user(db, seller=True)
    piece = make_piece(db, stranger, price_cents=300_00, status="live")
    event = make_event(db, host)

    response = client.post(
        f"/api/events/{event.id}/pieces",
        json={"pieceId": str(piece.id), "mode": "featured"},
        headers=auth_headers(stranger),
    )

    assert response.status_code == 404


# --- browse and detail --------------------------------------------------------------------------

def test_browsing_works_signed_out_because_entry_is_open(db, client):
    host = make_user(db, seller=True)
    make_event(db, host, status="published", title="Open to all")

    response = client.get("/api/events/browse")

    assert response.status_code == 200
    titles = [e["title"] for e in response.get_json()["data"]["upcoming"]]
    assert "Open to all" in titles


def test_a_draft_is_not_visible_to_anyone_but_its_host(db, client, auth_headers):
    host, stranger = make_user(db, seller=True), make_user(db)
    event = make_event(db, host, status="draft")

    assert client.get(f"/api/events/{event.id}").status_code == 404
    assert client.get(
        f"/api/events/{event.id}", headers=auth_headers(stranger)
    ).status_code == 404
    assert client.get(
        f"/api/events/{event.id}", headers=auth_headers(host)
    ).status_code == 200


def test_saving_an_event_is_reflected_back_to_the_saver_only(db, client, auth_headers):
    host, collector, other = make_user(db, seller=True), make_user(db), make_user(db)
    event = make_event(db, host, status="published")

    saved = client.post(f"/api/events/{event.id}/save", json={"saved": True},
                        headers=auth_headers(collector))

    assert saved.status_code == 200
    assert saved.get_json()["data"]["saveCount"] == 1
    mine = client.get(f"/api/events/{event.id}", headers=auth_headers(collector))
    theirs = client.get(f"/api/events/{event.id}", headers=auth_headers(other))
    assert mine.get_json()["data"]["saved"] is True
    assert theirs.get_json()["data"]["saved"] is False


def test_unsaving_removes_it(db, client, auth_headers):
    host, collector = make_user(db, seller=True), make_user(db)
    event = make_event(db, host, status="published")
    client.post(f"/api/events/{event.id}/save", json={"saved": True},
                headers=auth_headers(collector))

    response = client.post(f"/api/events/{event.id}/save", json={"saved": False},
                           headers=auth_headers(collector))

    assert response.get_json()["data"]["saveCount"] == 0


def test_the_following_feed_includes_events_a_followed_artist_is_billed_on(db, client, auth_headers):
    """Keying only on the host would hide exactly the events people care about — an artist
    they follow showing at someone else's gallery."""
    from tests.factories import make_event_participant
    from src.shared.models.social import Follow
    import uuid as _uuid

    gallery = make_user(db, seller=True)
    artist = make_user(db, seller=True)
    collector = make_user(db)
    db.add(Follow(id=_uuid.uuid4(), follower_id=collector.id, following_id=artist.id,
                  status="accepted"))
    db.commit()
    event = make_event(db, gallery, status="published", title="Group show")
    make_event_participant(db, event, artist, role="artist")

    response = client.get("/api/events?scope=following", headers=auth_headers(collector))

    assert response.status_code == 200
    assert "Group show" in [e["title"] for e in response.get_json()["data"]["events"]]


def test_hosting_scope_lists_only_the_viewers_own_events_drafts_included(db, client, auth_headers):
    host, someone_else = make_user(db, seller=True), make_user(db, seller=True)
    draft = make_event(db, host, title="Unpublished preview", status="draft")
    make_event(db, someone_else, title="Someone else's show", status="published")

    response = client.get("/api/events?scope=hosting", headers=auth_headers(host))

    assert response.status_code == 200
    titles = [e["title"] for e in response.get_json()["data"]["events"]]
    assert "Unpublished preview" in titles
    assert "Someone else's show" not in titles
    assert response.get_json()["data"]["events"][0]["id"] == str(draft.id)


def test_going_scope_lists_events_the_viewer_rsvpd_to(db, client, auth_headers):
    host, collector = make_user(db, seller=True), make_user(db)
    going = make_event(db, host, title="RSVP'd show", status="published")
    _skipped = make_event(db, host, title="Not going", status="published")
    client.post(f"/api/events/{going.id}/rsvp", json={"going": True},
                headers=auth_headers(collector))

    response = client.get("/api/events?scope=going", headers=auth_headers(collector))

    assert response.status_code == 200
    events = response.get_json()["data"]["events"]
    titles = [e["title"] for e in events]
    assert "RSVP'd show" in titles
    assert "Not going" not in titles
    assert events[0]["rsvpCount"] == 1


def test_setting_the_bill_replaces_rather_than_appends(db, client, auth_headers):
    host = make_user(db, seller=True)
    a, b = make_user(db), make_user(db)
    event = make_event(db, host)

    client.put(f"/api/events/{event.id}/people", json={"artistUsernames": [a.username]},
               headers=auth_headers(host))
    response = client.put(f"/api/events/{event.id}/people", json={"artistUsernames": [b.username]},
                          headers=auth_headers(host))

    assert response.status_code == 200
    artists = [p["username"] for p in response.get_json()["data"]["artists"]]
    assert artists == [b.username]


def test_the_host_is_never_listed_as_their_own_cohost(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = make_event(db, host)

    response = client.put(
        f"/api/events/{event.id}/people", json={"cohostUsernames": [host.username]},
        headers=auth_headers(host),
    )

    assert response.get_json()["data"]["cohosts"] == []


# --- the room's own screen ----------------------------------------------------------------

def test_an_auction_piece_on_the_bill_carries_its_live_state(db, client, auth_headers):
    """The screen people look at in the room is the event, not each piece in turn.

    Without live state the lineup could only ever show a starting price — which stops being
    true the moment somebody bids, and makes the room's own screen the least current thing
    in it.
    """
    from tests.factories import make_bid
    from src.shared.models.auction import Auction

    host, bidder = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, host, price_cents=300_00, status="draft")
    event = make_event(db, host)
    client.post(
        f"/api/events/{event.id}/pieces",
        json={"pieceId": str(piece.id), "mode": "bid", "priceCents": 400_00},
        headers=auth_headers(host),
    )
    client.post(f"/api/events/{event.id}/publish", headers=auth_headers(host))
    db.expire_all()
    auction = db.query(Auction).filter_by(piece_id=piece.id).one()
    make_bid(db, auction, bidder, 450_00)

    response = client.get(f"/api/events/{event.id}", headers=auth_headers(bidder))

    entry = response.get_json()["data"]["lineup"][0]
    assert entry["mode"] == "bid"
    live = entry["auction"]
    assert live["highestBidCents"] == 450_00
    assert live["bidCount"] == 1
    # Viewer-relative, so the room's screen can tell you you're winning.
    assert live["isHighestBidder"] is True
    assert live["auctionEndsAt"] is not None


def test_a_featured_piece_carries_no_auction_state(db, client, auth_headers):
    host = make_user(db, seller=True)
    piece = make_piece(db, host, price_cents=300_00, status="live")
    event = make_event(db, host, status="published")
    client.post(
        f"/api/events/{event.id}/pieces",
        json={"pieceId": str(piece.id), "mode": "featured"},
        headers=auth_headers(host),
    )

    response = client.get(f"/api/events/{event.id}", headers=auth_headers(host))

    entry = response.get_json()["data"]["lineup"][0]
    assert "auction" not in entry


def test_the_lineup_reports_who_won_after_the_event(db, client, auth_headers):
    """An event auction settles in the room, so the lineup has to keep showing the result
    rather than going blank the moment bidding closes."""
    from datetime import datetime, timedelta, timezone

    from src.modules.bids import auction_closer
    from src.shared.models.auction import Auction
    from tests.factories import make_bid

    host, bidder = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, host, price_cents=300_00, status="draft")
    event = make_event(db, host)
    client.post(
        f"/api/events/{event.id}/pieces",
        json={"pieceId": str(piece.id), "mode": "bid", "priceCents": 400_00},
        headers=auth_headers(host),
    )
    client.post(f"/api/events/{event.id}/publish", headers=auth_headers(host))
    db.expire_all()
    auction = db.query(Auction).filter_by(piece_id=piece.id).one()
    make_bid(db, auction, bidder, 500_00)
    # Wind the clock past the close.
    auction.closes_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    auction_closer.close_expired_auctions()

    response = client.get(f"/api/events/{event.id}", headers=auth_headers(bidder))

    live = response.get_json()["data"]["lineup"][0]["auction"]
    assert live["winningBidCents"] == 500_00
    assert live["isWinner"] is True
