"""Event routes.

Browse and detail are `optional_auth`: entry is free and open, so anyone can see what is on
without an account. Being signed in only adds viewer-relative facts — whether you saved it,
whether you host it.

Everything that writes is `onboarding_required`, matching pieces: creating an event puts work
on sale, and that needs a complete account behind it.
"""

from flask import Blueprint

from src.middlewares.auth_middleware import auth_required, onboarding_required, optional_auth
from src.shared.utils.api_response import success_response
from src.shared.utils.async_handler import async_handler
from src.modules.events import events_controller

events_bp = Blueprint("events", __name__)


def _ok(message, data, status=200):
    resp, _ = success_response(message, data, status)
    return resp, status


# --- browse -------------------------------------------------------------------------------

@events_bp.get("")
@optional_auth
@async_handler
def list_events():
    data, status = events_controller.list_events()
    return _ok("OK", data, status)


@events_bp.get("/browse")
@optional_auth
@async_handler
def browse():
    data, status = events_controller.browse()
    return _ok("OK", data, status)


@events_bp.get("/<event_id>")
@optional_auth
@async_handler
def get_event(event_id):
    data, status = events_controller.get_detail(event_id)
    return _ok("OK", data, status)


@events_bp.post("/<event_id>/rsvp")
@auth_required
@async_handler
def set_rsvp(event_id):
    """Say you're coming, or take it back. Free — an RSVP is a headcount, not a ticket."""
    data, status = events_controller.set_rsvp(event_id)
    return _ok("RSVP updated.", data, status)


@events_bp.get("/<event_id>/attendees")
@onboarding_required
@async_handler
def list_attendees(event_id):
    data, status = events_controller.list_attendees(event_id)
    return _ok("OK", data, status)


@events_bp.post("/<event_id>/save")
@auth_required
@async_handler
def save_event(event_id):
    data, status = events_controller.set_saved(event_id)
    return _ok("Saved.", data, status)


# --- host: create and edit ------------------------------------------------------------------

@events_bp.post("")
@onboarding_required
@async_handler
def create_event():
    data, status = events_controller.create()
    return _ok("Event created.", data, status)


@events_bp.patch("/<event_id>")
@onboarding_required
@async_handler
def patch_event(event_id):
    data, status = events_controller.patch(event_id)
    return _ok("Event updated.", data, status)


@events_bp.put("/<event_id>/people")
@onboarding_required
@async_handler
def set_people(event_id):
    data, status = events_controller.set_people(event_id)
    return _ok("Lineup updated.", data, status)


@events_bp.post("/<event_id>/publish")
@onboarding_required
@async_handler
def publish_event(event_id):
    data, status = events_controller.publish(event_id)
    return _ok("Event published.", data, status)


@events_bp.post("/<event_id>/cancel")
@onboarding_required
@async_handler
def cancel_event(event_id):
    data, status = events_controller.cancel(event_id)
    return _ok("Event cancelled.", data, status)


# --- the bill ---------------------------------------------------------------------------------

@events_bp.post("/<event_id>/pieces")
@onboarding_required
@async_handler
def add_lineup_piece(event_id):
    data, status = events_controller.add_lineup_piece(event_id)
    return _ok("Piece added to the bill.", data, status)


@events_bp.delete("/<event_id>/pieces/<piece_id>")
@onboarding_required
@async_handler
def remove_lineup_piece(event_id, piece_id):
    data, status = events_controller.remove_lineup_piece(event_id, piece_id)
    return _ok("Piece removed.", data, status)


@events_bp.get("/<event_id>/pieces/<piece_id>/tagging-preview")
@auth_required
@async_handler
def preview_piece_tagging(event_id, piece_id):
    """What adding this piece would end, so the host can be told before they do it."""
    data, status = events_controller.preview_piece_tagging(event_id, piece_id)
    return _ok("OK", data, status)
