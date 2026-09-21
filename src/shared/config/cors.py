"""CORS origin list for HTTP (flask-cors) and Socket.IO, plus refresh-cookie flags."""
import os
from urllib.parse import urlparse

# Local web (Vite 5173) and common aliases. Included automatically in development
# so a missing CORS_ORIGINS entry does not block the browser app.
_DEV_WEB_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5713",
    "http://127.0.0.1:5713",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


def cors_allowed_origins() -> list[str]:
    """FRONTEND_URL plus extra CORS_ORIGINS, and local web ports in development."""
    origins: list[str] = []
    frontend = (os.getenv("FRONTEND_URL") or "http://localhost:5173").strip()
    extra = (os.getenv("CORS_ORIGINS") or "").strip()
    for raw in (frontend, extra):
        for part in raw.split(","):
            origin = part.strip().rstrip("/")
            if origin and origin not in origins:
                origins.append(origin)
    if os.getenv("FLASK_ENV", "development") == "development":
        for origin in _DEV_WEB_ORIGINS:
            if origin not in origins:
                origins.append(origin)
    return origins


def refresh_cookie_flags(request_obj=None) -> dict:
    """Cookie flags so the httpOnly refreshToken works for the web app.

    Native clients (no Origin) keep SameSite=Lax.
    Credentialed browser fetch from a CORS origin on a different host:port
    (e.g. localhost:5173 → localhost:9000) needs SameSite=None; Secure.
    Chromium treats http://localhost as a secure context, so Secure works locally.
    """
    flags = {"httponly": True, "path": "/", "samesite": "Lax"}
    # Any deployed environment is served over HTTPS, so the refresh cookie must be Secure
    # there. Checking for "production" specifically would have silently shipped an insecure
    # refresh cookie on staging the moment that environment existed.
    if os.getenv("FLASK_ENV", "development") != "development":
        flags["secure"] = True

    req = request_obj
    if req is None:
        from flask import has_request_context, request as flask_request

        if has_request_context():
            req = flask_request
    if req is None:
        return flags

    origin = (req.headers.get("Origin") or "").strip().rstrip("/")
    if not origin:
        return flags

    allowed = {item.lower() for item in cors_allowed_origins()}
    if origin.lower() not in allowed:
        return flags

    origin_host = urlparse(origin).netloc.lower()
    api_host = (req.host or "").lower()
    if origin_host and origin_host != api_host:
        flags["samesite"] = "None"
        flags["secure"] = True
    return flags
