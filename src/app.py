"""Flask app: middleware (CORS, JSON), API blueprints, global error handler."""
import os
from flask import Flask
from flask_cors import CORS

from src.middlewares.error_handler import register_error_handler
from src.shared.realtime.socketio_instance import socketio

def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-me")
    app.config["JSON_SORT_KEYS"] = False

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
