"""Alembic environment: uses src shared database and models (no Flask)."""
import os
from pathlib import Path

# Load .env so DATABASE_URL is set when running: alembic upgrade head
#
# The environment wins over the files (override=False), as in run.py. With override=True a
# DATABASE_URL set in the shell was silently replaced by .env.development's, so a migration
# meant for a local database ran against the shared development one instead.
from dotenv import load_dotenv
_base = Path(__file__).resolve().parent.parent
load_dotenv(_base / ".env", override=False)
_env = os.getenv("FLASK_ENV", "development")
_env_file = _base / f".env.{_env}"
if _env_file.exists():
    load_dotenv(_env_file, override=False)

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from src.shared.config.database import engine, Base
import src.shared.models  # noqa: F401 - discover all models

config = context.config
if config.config_file_name:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass

target_metadata = Base.metadata


def get_url():
    from src.shared.config.database import get_database_url
    return get_database_url()


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import create_engine
    connectable = create_engine(
        get_url(),
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
