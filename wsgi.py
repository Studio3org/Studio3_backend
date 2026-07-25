"""WSGI entry for gunicorn (production)."""
# Must be first — before dotenv/threading/socketio — or eventlet raises
# "RLock(s) were not greened" when Flask-SocketIO loads with async_mode=eventlet.
import eventlet

eventlet.monkey_patch()

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
# Never override process env (Render/Docker inject real secrets there).
# override=True would let an empty .env.production wipe AWS_* at boot.
load_dotenv(BASE / ".env", override=False)
env = os.getenv("FLASK_ENV", "production")
load_dotenv(BASE / f".env.{env}", override=False)

sys.path.insert(0, str(BASE))

from src.app import create_app

app = create_app()
