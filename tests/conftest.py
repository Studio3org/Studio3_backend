"""Shared fixtures.

Three decisions here are load-bearing and non-obvious; each is explained at its fixture:

1. Real Postgres, not SQLite (see `engine`).
2. Schema from `alembic upgrade head`, not `Base.metadata.create_all` (see `migrated_database`).
3. TRUNCATE between tests, not a rolled-back outer transaction (see `clean_db`).

The gevent monkey-patch that run.py and wsgi.py apply is deliberately NOT applied here.
alembic/env.py already runs unpatched, and patching would turn the concurrency tests into
cooperative greenlets that never actually contend for a row lock — which is the one thing
those tests exist to prove. Consequence: Socket.IO handlers are out of scope for this suite.
"""
from pathlib import Path
from typing import Callable, Iterator

import pytest
import redis as redis_lib
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.shared.config.database import Base, get_database_url, get_engine
from src.shared.config.redis_client import get_redis_client, redis_url
from src.shared.models.ledger import PLATFORM_ACCOUNT_TYPES
import src.shared.models  # noqa: F401 — registers every table on Base.metadata

BASE_DIR = Path(__file__).resolve().parent.parent
TEST_DB_MARKER = "studio3_test"

# Built once at import: the table list never changes within a run, and rebuilding the string
# on every teardown is pure waste.
_TRUNCATE_TABLES = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)


# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------

@pytest.fixture(scope="session")
def database_url() -> str:
    """The test database URL, with a guard against ever pointing at a real one.

    `clean_db` TRUNCATEs every table between tests, so running against a developer's
    database would destroy their data silently. Two things can cause that: TEST_DATABASE_URL
    set to the wrong thing, or a bare `.env` in the repo root, which alembic/env.py loads
    with override=True and which would therefore win over what conftest.py set.
    """
    if (BASE_DIR / ".env").exists():
        pytest.exit(
            "A bare .env exists in Backend/Server. alembic/env.py loads it with "
            "override=True, so it would redirect the test run at whatever DATABASE_URL it "
            "contains — and the suite TRUNCATEs between tests. Remove it, or move its "
            "contents into .env.development.",
            returncode=1,
        )

    url = get_database_url()
    if TEST_DB_MARKER not in url:
        pytest.exit(
            f"Refusing to run: DATABASE_URL does not name a test database "
            f"(expected {TEST_DB_MARKER!r} in it). Set TEST_DATABASE_URL.",
            returncode=1,
        )
    return url


@pytest.fixture(scope="session", autouse=True)
def migrated_database(database_url: str) -> None:
    """Build the schema by running the real migration chain.

    NOT `Base.metadata.create_all`, for three reasons:

    - Migration 022 bulk-inserts the six platform ledger accounts. create_all leaves
      `ledger_accounts` empty, so every test would silently exercise the defensive
      get-or-create fallback in ledger_service.get_platform_account — a path that must
      never run in production.
    - render.yaml runs `alembic upgrade head` in the start command, so a broken chain is a
      production outage. Executing it here means CI catches that.
    - 28 hand-written revisions carry index and constraint DDL that create_all won't
      reproduce, which would hide model/migration drift.
    """
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()

    engine = get_engine()
    if _current_revision(engine) != head:
        # Virgin database, or the chain moved. Rebuild from scratch so a renamed or edited
        # revision can never leave a half-migrated schema behind.
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        command.upgrade(config, "head")

    # alembic/env.py re-reads dotenv; make sure it didn't move us off the test database.
    assert TEST_DB_MARKER in get_database_url(), "DATABASE_URL changed during migration"


def _current_revision(engine: Engine) -> str | None:
    """The revision this database is stamped at, or None if it has never been migrated.

    Re-running 28 migrations costs ~30s against a hosted Postgres, so a local re-run skips
    it when the schema is already current. CI always starts from an empty database, so it
    still exercises the full chain on every push — which is the point of running it there.
    """
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT to_regclass('public.alembic_version')")
        ).scalar_one()
        if exists is None:
            return None
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()


@pytest.fixture(scope="session")
def engine(migrated_database) -> Engine:
    """One engine for the session.

    Postgres, not SQLite, and not negotiable: the models use postgresql UUID/JSONB/ARRAY
    throughout; `with_for_update()` is a silent no-op on SQLite, which would make every
    row-lock test pass without proving anything; and migration 022 creates a *partial*
    unique index for the platform singletons that only Postgres honours.
    """
    return get_engine()


@pytest.fixture(autouse=True)
def clean_db(engine: Engine) -> Iterator[None]:
    """Truncate between tests and restore the migration's seed data.

    The usual "outer transaction + rollback" isolation does not work for this codebase.
    webhook_handler, payouts_service, auction_closer and every controller open their own
    SessionLocal() and commit; those sessions take different connections from the pool and
    cannot see an uncommitted outer transaction. payouts_service goes further and depends on
    a real COMMIT releasing the FOR UPDATE lock it took in step 1 before it calls Stripe.

    Forcing every session onto one connection would fix visibility but make with_for_update
    self-locking — destroying exactly the tests worth having. So: let the code commit for
    real, and clean up afterwards. ~10-20ms for ~40 tables.
    """
    yield
    with engine.begin() as conn:
        # One statement each, not one per table and one per seed row: every round trip is a
        # TLS hop to a hosted Postgres, and teardown runs after every single test.
        conn.execute(text(f"TRUNCATE {_TRUNCATE_TABLES} RESTART IDENTITY CASCADE"))
        # Re-seed what migration 022 bulk-inserted; ledger_service treats a missing platform
        # account as a bug it has to work around, and we want that path to stay unreached.
        conn.execute(
            text(
                "INSERT INTO ledger_accounts (id, type, owner_id, currency, created_at) "
                "SELECT gen_random_uuid(), t, NULL, 'USD', now() "
                "FROM unnest(CAST(:types AS text[])) AS t"
            ),
            {"types": list(PLATFORM_ACCOUNT_TYPES)},
        )


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """A plain session whose commits are real, matching how controllers use SessionLocal."""
    from src.shared.config.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# Redis
# --------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def flush_redis() -> Iterator[redis_lib.Redis]:
    """Real Redis on a dedicated db index, flushed between tests.

    Real rather than faked because auth_middleware fails *open* when Redis raises, and that
    behaviour deserves to be pinned against a real client rather than a stub's idea of one.
    """
    assert redis_url().rstrip("/").endswith(("/15", "/14")), (
        f"Refusing to flush {redis_url()} — tests expect a dedicated db index (15)."
    )
    client = get_redis_client()
    client.flushdb()
    yield client
    client.flushdb()


# --------------------------------------------------------------------------------------
# App / HTTP
# --------------------------------------------------------------------------------------

@pytest.fixture(scope="session")
def app(migrated_database) -> Flask:
    from src.app import create_app

    # TESTING suppresses the APScheduler thread; without the config_overrides parameter
    # added in this change there was no way to set it before create_app read it.
    return create_app({"TESTING": True, "WTF_CSRF_ENABLED": False})


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def auth_headers(flush_redis) -> Callable[[object], dict]:
    """Build real Authorization headers for a user.

    Mints a genuine Redis session and signs a genuine JWT rather than patching `g.user`, so
    the auth middleware is exercised end to end — including the Redis session lookup, which
    is where a revoked session is supposed to be caught.
    """
    from src.modules.sessions import session_service
    from src.shared.utils.jwt_utils import sign_access_token

    def _headers(user) -> dict:
        session_id = session_service.create_session(str(user.id))
        token = sign_access_token({"sub": str(user.id), "sessionId": session_id})
        return {"Authorization": f"Bearer {token}"}

    return _headers


# --------------------------------------------------------------------------------------
# Stripe
# --------------------------------------------------------------------------------------

@pytest.fixture
def stripe_stub(monkeypatch):
    """Install a fake Stripe at every *use* site.

    webhook_handler, payouts_service, disputes_service and payments_controller each do
    `from ...stripe_client import get_stripe`, which binds the name into their own module
    namespace — so patching the source module has no effect on them. Each importer has to be
    patched by name.
    """
    from tests.stripe_fixtures import StripeStub

    stub = StripeStub()
    for target in (
        "src.modules.payments.webhook_handler",
        "src.modules.payments.payouts_service",
        "src.modules.payments.payments_controller",
        "src.modules.admin.disputes_service",
        # Holds are a separate Stripe code path (manual capture), and the auction lifecycle
        # is entirely built on it.
        "src.modules.bids.holds_service",
        "src.modules.payments.payment_methods_controller",
        # Connect onboarding. Absent from this list until the Accounts v2 move, which is why
        # the entire onboarding surface had no tests.
        "src.modules.connect.connect_controller",
        # The abandoned-checkout sweep asks Stripe to cancel the intent before releasing.
        "src.modules.orders.stale_orders",
    ):
        monkeypatch.setattr(f"{target}.get_stripe", lambda _s=stub: _s, raising=False)
    # Accounts v2 goes through a separate client, so it needs its own patch.
    monkeypatch.setattr(
        "src.modules.connect.connect_controller.get_stripe_v2", lambda _s=stub: _s,
        raising=False,
    )
    return stub


@pytest.fixture
def stripe_enabled(monkeypatch, stripe_stub):
    """As `stripe_stub`, but also makes stripe_configured() report True.

    Without this the code takes its dev-mode auto-succeed branches, which are a different
    thing worth testing separately.
    """
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    for target in (
        "src.modules.payments.payouts_service",
        "src.modules.payments.payments_controller",
        "src.modules.bids.holds_service",
        "src.modules.payments.payment_methods_controller",
        "src.modules.connect.connect_controller",
        "src.modules.orders.stale_orders",
    ):
        monkeypatch.setattr(f"{target}.stripe_configured", lambda: True, raising=False)
    return stripe_stub
