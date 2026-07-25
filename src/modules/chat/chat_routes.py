"""Chat routes — general-purpose 1:1 direct messages, mounted at /api/conversations."""
from flask import Blueprint

from src.middlewares.auth_middleware import auth_required, onboarding_required
from src.shared.utils.api_response import success_response
from src.shared.utils.async_handler import async_handler
from src.modules.chat import chat_controller

chat_bp = Blueprint("chat", __name__)


def _ok(message, data, status=200):
    resp, _ = success_response(message, data, status)
    return resp, status


@chat_bp.get("")
@auth_required
@async_handler
def list_inbox():
    data, status = chat_controller.list_inbox()
    return _ok("OK", data, status)


@chat_bp.get("/requests")
@auth_required
@async_handler
def list_requests():
    data, status = chat_controller.list_requests()
    return _ok("OK", data, status)


@chat_bp.get("/search-users")
@auth_required
@async_handler
def search_users():
    data, status = chat_controller.search_users()
    return _ok("OK", data, status)


@chat_bp.get("/unread-count")
@auth_required
@async_handler
def unread_count():
    data, status = chat_controller.unread_count()
    return _ok("OK", data, status)


@chat_bp.get("/with/<username>")
@auth_required
@async_handler
def find_with_user(username):
    """Return the open/pending thread with this user, if any (for compose → reuse)."""
    data, status = chat_controller.find_with_user(username)
    return _ok("OK", data, status)


@chat_bp.get("/<conversation_id>")
@auth_required
@async_handler
def get_thread(conversation_id):
    data, status = chat_controller.get_thread(conversation_id)
    return _ok("OK", data, status)


@chat_bp.post("")
@onboarding_required
@async_handler
def start_conversation():
    data, status = chat_controller.start_conversation()
    return _ok("Message sent.", data, status)


@chat_bp.post("/<conversation_id>/messages")
@onboarding_required
@async_handler
def send_message(conversation_id):
    data, status = chat_controller.send_message(conversation_id)
    return _ok("Message sent.", data, status)


@chat_bp.post("/<conversation_id>/accept")
@auth_required
@async_handler
def accept(conversation_id):
    data, status = chat_controller.accept(conversation_id)
    return _ok("Request accepted.", data, status)


@chat_bp.post("/<conversation_id>/decline")
@auth_required
@async_handler
def decline(conversation_id):
    data, status = chat_controller.decline(conversation_id)
    return _ok("Request declined.", data, status)


@chat_bp.patch("/<conversation_id>/read")
@auth_required
@async_handler
def mark_read(conversation_id):
    data, status = chat_controller.mark_read(conversation_id)
    return _ok("Marked read.", data, status)
