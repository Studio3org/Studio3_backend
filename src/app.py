"""Flask app: middleware (CORS, JSON), API blueprints, global error handler."""
import os
from datetime import timedelta

from flask import Flask
from flask_cors import CORS
from flask_wtf.csrf import CSRFProtect

from src.middlewares.error_handler import register_error_handler
from src.middlewares.request_context import init_request_context
from src.shared.config.cors import cors_allowed_origins
from src.shared.config.sentry import init_sentry
from src.shared.config.settings import require_environment, warn_unset_optional
from src.shared.realtime.socketio_instance import socketio, socketio_options

csrf = CSRFProtect()


def _secret_key() -> str:
    """SECRET_KEY signs the admin session cookie, which gates refunds and payout releases.

    Outside development a missing key is already fatal via require_environment, which reports
    it alongside anything else that is missing; this only supplies the local default.
    """
    key = (os.getenv("SECRET_KEY") or "").strip()
    if key:
        return key
    if os.getenv("FLASK_ENV", "development") != "development":
        raise RuntimeError("SECRET_KEY must be set outside development.")
    return "dev-only-insecure-key"


def create_app(config_overrides: dict | None = None):
    """Build the Flask app.

    `config_overrides` is applied before anything reads app.config, so a caller can set
    TESTING (which suppresses the scheduler below) or WTF_CSRF_ENABLED. Without it the
    TESTING check further down was unreachable — nothing could set the flag in time.
    """
    env = os.getenv("FLASK_ENV", "development")
    # Before anything else: a deploy missing required configuration should stop here, with
    # every problem named at once, rather than start and fail later in a way that looks
    # healthy from the outside.
    require_environment(env)
    # Before the app exists, so an error raised during setup is still captured. No-op unless
    # SENTRY_DSN is set, which is why dev and CI need no special handling.
    init_sentry(env)

    app = Flask(__name__)
    app.config["SECRET_KEY"] = _secret_key()
    app.config["JSON_SORT_KEYS"] = False
    app.config.update(config_overrides or {})

    # Admin session cookies (browser-only /admin UI). The mobile API is bearer-token based
    # and unaffected by these.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_ENV", "development") != "development"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

    # Credentialed CORS for the web app (Vite :5173) and mobile/web on :3000.
    CORS(
        app,
        origins=cors_allowed_origins(),
        supports_credentials=True,
        allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )

    # Blueprints
    from src.modules.auth.auth_routes import auth_bp
    from src.modules.user.user_routes import user_bp, media_bp
    from src.modules.user.users_content_routes import users_bp
    from src.modules.pieces.pieces_routes import pieces_bp
    from src.modules.posts.posts_routes import posts_bp
    from src.modules.social.social_routes import social_bp
    from src.modules.reports.report_routes import reports_bp
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
    from src.modules.share.share_routes import share_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(user_bp, url_prefix="/api/user")
    app.register_blueprint(users_bp, url_prefix="/api/users")
    app.register_blueprint(media_bp, url_prefix="/api/media")
    app.register_blueprint(pieces_bp, url_prefix="/api/pieces")
    app.register_blueprint(posts_bp, url_prefix="/api/posts")
    app.register_blueprint(social_bp, url_prefix="/api")
    app.register_blueprint(reports_bp, url_prefix="/api")
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
    # Public share/OG-preview pages: HTML, no auth, deliberately outside the /api prefix.
    app.register_blueprint(share_bp, url_prefix="/share")

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
    # @socketio.on(...) handlers (import has the side effect of registering them). The
    # server options are resolved here rather than at import so the Redis pub/sub backend
    # reads the environment this app was actually configured with.
    socketio.init_app(app, **socketio_options())
    from src.modules.chat import chat_socket  # noqa: F401

    # Liveness. Deliberately touches nothing external: Render pings this, and a transient
    # database blip must not be read as "this process is dead, restart it".
    @app.get("/")
    def health():
        return {"message": "Studiothree Discover API running", "status": "ok"}, 200

    # Readiness and diagnostics. Point an uptime monitor here, not Render's health check —
    # Redis fails open throughout this app, so "degraded" still means serving traffic.
    @app.get("/health")
    def health_detail():
        from src.shared.config.database import check_db_connection
        from src.shared.config.redis_client import check_redis_connection
        from src.shared.config.stripe_client import stripe_configured, webhook_secrets
        from src.shared.storage.s3_client import get_bucket, s3_configured

        db_ok, db_error = check_db_connection()
        redis_ok = check_redis_connection()
        ok = db_ok and redis_ok
        return {
            "status": "ok" if ok else "degraded",
            "environment": env,
            "checks": {
                # The error string can contain the connection URL, so it is only ever
                # returned locally.
                "database": {
                    "ok": db_ok,
                    # Local only. Staging is internet-facing too, and this endpoint has no
                    # auth, so the error text — which can carry the connection string — is
                    # withheld from every deployed environment. Read it from the logs there.
                    "error": db_error if not db_ok and env == "development" else None,
                },
                "redis": {"ok": redis_ok},
                "stripe": {
                    "configured": stripe_configured(),
                    "webhookSecretSet": bool(webhook_secrets()),
                },
                # Booleans only — never the values.
                "s3": {"configured": s3_configured(), "bucketSet": bool(get_bucket())},
            },
        }, (200 if ok else 503)


    # Correlation ids, registered before the error handler so a 500's traceback carries the
    # same id as the access line next to it.
    init_request_context(app)

    # Global error handler (register last)
    register_error_handler(app)

    if not app.config.get("TESTING"):
        warn_unset_optional(env)

    # Scheduled work (auction close, hold refresh, waitlist expiry, reconciliation) runs in
    # the Celery worker, not here — see src/jobs/celery_app.py. The web process starts no
    # background threads of its own.

    return app
