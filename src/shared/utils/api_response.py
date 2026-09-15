"""Success and error response helpers - same shape as reference."""

from flask import jsonify
from typing import Any, Optional

from src.shared.utils.messages import INTERNAL_SERVER_ERROR


def success_response(
    message: str,
    data: Any = None,
    status_code: int = 200,
):
    """Return JSON: { success: true, message, data? }."""
    body = {"success": True, "message": message}
    if data is not None:
        body["data"] = data
    return jsonify(body), status_code


def error_response(
    message: str,
    status_code: int = 500,
):
    """Return JSON: { success: false, message }."""
    return jsonify({"success": False, "message": message}), status_code


def internal_error_response():
    """Generic 500 response."""
    return error_response(INTERNAL_SERVER_ERROR, 500)


def apply_cookie_ops(resp, cookie_ops):
    """Apply a controller's requested cookie mutation (set/clear the
    refreshToken cookie) to a Flask response — shared by any route wrapper
    that forwards a (data, status, cookie_ops) tuple from a controller."""
    if not cookie_ops:
        return
    if cookie_ops.get("set_refresh_cookie"):
        opts = cookie_ops["set_refresh_cookie"].copy()
        value = opts.pop("value")
        resp.set_cookie("refreshToken", value, **opts)
    if cookie_ops.get("clear_refresh_cookie"):
        flags = cookie_ops["clear_refresh_cookie"]
        if flags is True:
            resp.delete_cookie("refreshToken", path="/")
        else:
            delete_kw = {
                k: flags[k]
                for k in ("path", "samesite", "secure", "domain")
                if k in flags
            }
            resp.delete_cookie("refreshToken", **delete_kw)
