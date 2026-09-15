"""Auth Blueprint: /api/auth."""
from flask import Blueprint

from src.shared.utils.api_response import success_response, apply_cookie_ops
from src.shared.utils.messages import (
    OTP_SENT,
    LOGIN_SUCCESS,
    LOGOUT_SUCCESS,
    LOGOUT_ALL_SUCCESS,
    REFRESH_SUCCESS,
    PASSWORD_RESET_EMAIL_SENT,
    PASSWORD_RESET_SUCCESS,
)
from src.middlewares.auth_middleware import auth_required, optional_auth
from src.modules.auth import auth_controller

auth_bp = Blueprint("auth", __name__)


def _respond(message, data, status, cookie_ops):
    resp, _ = success_response(message, data, status)
    apply_cookie_ops(resp, cookie_ops)
    return resp, status


@auth_bp.get("/username/check")
@optional_auth
def username_check():
    data, status, cookie_ops = auth_controller.username_check()
    return _respond("OK", data, status, cookie_ops)


@auth_bp.post("/otp/generate")
def otp_generate():
    data, status, cookie_ops = auth_controller.otp_generate()
    return _respond(OTP_SENT, data, status, cookie_ops)


@auth_bp.post("/otp/resend")
def otp_resend():
    data, status, cookie_ops = auth_controller.otp_resend()
    return _respond(OTP_SENT, data, status, cookie_ops)


@auth_bp.post("/otp/verify")
def otp_verify():
    data, status, cookie_ops = auth_controller.otp_verify()
    return _respond("OTP verified.", data, status, cookie_ops)


@auth_bp.post("/register")
def register():
    data, status, cookie_ops = auth_controller.register()
    return _respond(LOGIN_SUCCESS, data, status, cookie_ops)


@auth_bp.post("/login")
def login():
    data, status, cookie_ops = auth_controller.login()
    return _respond(LOGIN_SUCCESS, data, status, cookie_ops)


@auth_bp.post("/refresh")
def refresh():
    data, status, cookie_ops = auth_controller.refresh()
    return _respond(REFRESH_SUCCESS, data, status, cookie_ops)


@auth_bp.post("/logout")
def logout():
    data, status, cookie_ops = auth_controller.logout()
    return _respond(LOGOUT_SUCCESS, data, status, cookie_ops)


@auth_bp.post("/logout-all")
@auth_required
def logout_all():
    data, status, cookie_ops = auth_controller.logout_all()
    return _respond(LOGOUT_ALL_SUCCESS, data, status, cookie_ops)


@auth_bp.post("/forget-password")
def forget_password():
    data, status, cookie_ops = auth_controller.forget_password()
    return _respond(PASSWORD_RESET_EMAIL_SENT, data, status, cookie_ops)


@auth_bp.post("/reset-password")
def reset_password():
    data, status, cookie_ops = auth_controller.reset_password()
    return _respond(PASSWORD_RESET_SUCCESS, data, status, cookie_ops)
