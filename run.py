"""Entry: load env (by FLASK_ENV), connect DB + Redis, then run Flask app. Exit on connection failure."""
# Must run before any other import (sockets/threading). Production wsgi.py does the same.
from gevent import monkey
monkey.patch_all()

import os
import sys
from pathlib import Path

# Load env before any app/config imports
BASE_DIR = Path(__file__).resolve().parent
from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env", override=False)
env_name = os.getenv("FLASK_ENV", "development")
env_file = BASE_DIR / f".env.{env_name}"
if env_file.exists():
    # override=False everywhere, matching wsgi.py and alembic/env.py: an explicitly exported
    # variable is the most specific instruction available and must win over a file of
    # defaults. This used to override in development, which meant a local run could not be
    # pointed at another database or Redis without editing the file — the export was read,
    # then silently discarded, and the process ran against something else entirely.
    load_dotenv(env_file, override=False)

# Ensure project root on path
sys.path.insert(0, str(BASE_DIR))

from src.shared.config.database import check_db_connection
from src.shared.config.redis_client import check_redis_connection, close_redis
from src.shared.utils.logger import get_logger

logger = get_logger("run")

def _print_err(message: str) -> None:
    # Printed directly to stderr (unconditional, unbuffered) rather than only through
    # `logger` — the logger's console handler is only attached when FLASK_ENV resolves to
    # "development" (src/shared/utils/logger.py), so a stale FLASK_ENV in the parent shell
    # can silently suppress every logger.error() call with no visible sign anything is wrong.
    print(message, file=sys.stderr, flush=True)


def main():
    db_ok, db_err = check_db_connection()
    if not db_ok:
        _print_err("Database connection failed. Check DATABASE_URL.")
        if db_err:
            _print_err(f"DB error: {db_err}")
        logger.error("Database connection failed. Check DATABASE_URL.")
        if db_err:
            logger.error("DB error: %s", db_err)
        sys.exit(1)
    if not check_redis_connection():
        _print_err("Redis connection failed. Check REDIS_URL.")
        logger.error("Redis connection failed. Check REDIS_URL.")
        sys.exit(1)

    from src.app import create_app
    from src.shared.realtime.socketio_instance import socketio
    app = create_app()

    port = int(os.getenv("PORT", 9000))
    try:
        # socketio.run (not app.run) so the /socket.io websocket endpoint works in dev too.
        # Unlike Flask's own dev server, this doesn't print a "Running on http://..." banner,
        # so print it explicitly — otherwise a successful start looks identical to a silent hang.
        _print_err(f"Server listening on http://0.0.0.0:{port} (Socket.IO enabled)")
        logger.info("Server listening on http://0.0.0.0:%d (Socket.IO enabled)", port)
        socketio.run(app, host="0.0.0.0", port=port)
    finally:
        close_redis()

if __name__ == "__main__":
    main()
