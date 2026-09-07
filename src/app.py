"""Flask app: middleware (CORS, JSON), API blueprints, global error handler."""
import os
from datetime import timedelta

from flask import Flask
from flask_cors import CORS
from flask_wtf.csrf import CSRFProtect

from src.middlewares.error_handler import register_error_handler
from src.shared.realtime.socketio_instance import socketio

csrf = CSRFProtect()


def _secret_key() -> str:
    """SECRET_KEY signs the admin session cookie, which gates refunds and payout releases.
    A default value in production would let anyone forge an admin session, so fail at boot
    rather than start up insecure."""
    key = (os.getenv("SECRET_KEY") or "").strip()
    if key:
        return key
    if os.getenv("FLASK_ENV", "development") != "development":
        raise RuntimeError("SECRET_KEY must be set outside development.")
    return "dev-only-insecure-key"


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = _secret_key()
    app.config["JSON_SORT_KEYS"] = False

    # Admin session cookies (browser-only /admin UI). The mobile API is bearer-token based
    # and unaffected by these.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_ENV", "development") != "development"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

    # CORS: allow frontend origin
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    CORS(app, origins=[frontend_url], supports_credentials=True)

    # Blueprints
    from src.modules.auth.auth_routes import auth_bp
    from src.modules.user.user_routes import user_bp, media_bp
    from src.modules.user.users_content_routes import users_bp
    from src.modules.pieces.pieces_routes import pieces_bp
    from src.modules.posts.posts_routes import posts_bp
    from src.modules.social.social_routes import social_bp
    from src.modules.feeds.feeds_routes import feeds_bp
    from src.modules.series.series_routes import series_bp
    from src.modules.notifications.notifications_routes import notifications_bp
    # Inquiries deferred to v2 in favor of general-purpose chat (see src/modules/chat). Left
    # unregistered rather than deleted so the feature can be re-enabled later.
    # from src.modules.inquiries.inquiries_routes import inquiries_bp
    from src.modules.orders.orders_routes import orders_bp
    from src.modules.chat.chat_routes import chat_bp
    from src.modules.collections.collections_routes import collections_bp
    from src.modules.admin.admin_routes import admin_bp
    from src.modules.payments.payments_routes import payments_bp
    from src.modules.connect.connect_routes import connect_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(user_bp, url_prefix="/api/user")
    app.register_blueprint(users_bp, url_prefix="/api/users")
    app.register_blueprint(media_bp, url_prefix="/api/media")
    app.register_blueprint(pieces_bp, url_prefix="/api/pieces")
    app.register_blueprint(posts_bp, url_prefix="/api/posts")
    app.register_blueprint(social_bp, url_prefix="/api")
    app.register_blueprint(feeds_bp, url_prefix="/api/feed")
    app.register_blueprint(series_bp, url_prefix="/api/series")
    app.register_blueprint(notifications_bp, url_prefix="/api/notifications")
    # app.register_blueprint(inquiries_bp, url_prefix="/api/inquiries")
    app.register_blueprint(orders_bp, url_prefix="/api/orders")
    app.register_blueprint(chat_bp, url_prefix="/api/conversations")
    app.register_blueprint(collections_bp, url_prefix="/api/collections")
    app.register_blueprint(payments_bp, url_prefix="/api/payments")
    app.register_blueprint(connect_bp, url_prefix="/api/artists")
    # Internal ops UI: HTML, session-cookie auth, deliberately outside the /api prefix.
    app.register_blueprint(admin_bp, url_prefix="/admin")

    # CSRF applies only to the cookie-authenticated admin forms. The JSON API authenticates
    # with a bearer token that a cross-site form post cannot supply, so blanket checking is
    # off (it would break every mobile client) and admin writes opt in below.
    app.config["WTF_CSRF_CHECK_DEFAULT"] = False
    csrf.init_app(app)

    @app.before_request
    def _csrf_protect_admin():
        from flask import request

        if request.blueprint == "admin" and request.method not in ("GET", "HEAD", "OPTIONS"):
            csrf.protect()

    # Real-time chat: binds the shared SocketIO instance to this app and registers its
    # @socketio.on(...) handlers (import has the side effect of registering them).
    socketio.init_app(app)
    from src.modules.chat import chat_socket  # noqa: F401

    # Health at root — includes S3 env presence (booleans only, no secret values)
    # so Render misconfig is visible without digging through logs.
    @app.get("/")
    def health():
        from src.shared.storage.s3_client import s3_configured, get_bucket

        return {
            "message": "Studiothree Discover API running",
            "s3": {
                "configured": s3_configured(),
                "bucketSet": bool(get_bucket()),
                "accessKeySet": bool((os.getenv("AWS_ACCESS_KEY_ID") or "").strip()),
                "secretKeySet": bool((os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()),
                "publicBaseUrlSet": bool(
                    (os.getenv("S3_PUBLIC_BASE_URL") or "").strip()
                ),
            },
        }, 200


    # Global error handler (register last)
    register_error_handler(app)



    return app
