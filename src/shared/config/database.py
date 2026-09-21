"""SQLAlchemy 2.0 engine, session factory, and declarative Base."""
import os
from typing import Generator
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session


def _sanitize_database_url(url: str) -> str:
    """Normalize URL for psycopg2 (postgres:// → postgresql://, strip pgbouncer param)."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    parsed = urlparse(url)
    if not parsed.query:
        return url
    params = parse_qs(parsed.query, keep_blank_values=True)
    params.pop("pgbouncer", None)
    if not params:
        return urlunparse(parsed._replace(query=""))
    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def get_database_url() -> str:
    return _sanitize_database_url(
        os.getenv("DATABASE_URL", "postgresql://localhost:5432/flask_app_dev")
    )


_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_database_url(),
            pool_pre_ping=True,
            pool_recycle=300,
            pool_size=10,
            max_overflow=20,
            # Without this a hung Postgres blocks until the OS TCP timeout (~2 min), which
            # would make the /health readiness probe hang rather than report degraded.
            connect_args={"connect_timeout": 5},
        )
    return _engine


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    return _session_factory


class _EngineProxy:
    """Lazy proxy — engine is created after dotenv loads."""

    def __getattr__(self, name):
        return getattr(get_engine(), name)


class _SessionLocalProxy:
    def __call__(self, *args, **kwargs):
        return get_session_factory()(*args, **kwargs)


engine = _EngineProxy()
SessionLocal = _SessionLocalProxy()


class Base(DeclarativeBase):
    """Declarative base for all models."""

    pass


def get_db() -> Generator[Session, None, None]:
    """Yield a DB session; caller must close/commit/rollback."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_db_connection() -> tuple[bool, str | None]:
    """Run SELECT 1 to verify DB is reachable. Returns (ok, error_message)."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:
        return False, str(exc)
