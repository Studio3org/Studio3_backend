"""DATABASE_URL must resolve to the installed psycopg2 driver."""
from src.shared.config.database import _sanitize_database_url


def test_postgres_scheme_becomes_psycopg2():
    assert (
        _sanitize_database_url("postgres://u:p@host:5432/db")
        == "postgresql+psycopg2://u:p@host:5432/db"
    )


def test_bare_postgresql_is_pinned_to_psycopg2():
    assert (
        _sanitize_database_url("postgresql://u:p@host:5432/db?sslmode=require")
        == "postgresql+psycopg2://u:p@host:5432/db?sslmode=require"
    )


def test_explicit_psycopg3_is_rewritten():
    assert (
        _sanitize_database_url("postgresql+psycopg://u:p@host/db")
        == "postgresql+psycopg2://u:p@host/db"
    )


def test_already_psycopg2_is_unchanged():
    url = "postgresql+psycopg2://u:p@host:5432/db"
    assert _sanitize_database_url(url) == url


def test_pgbouncer_query_flag_is_stripped():
    assert (
        _sanitize_database_url("postgresql://u:p@host/db?sslmode=require&pgbouncer=true")
        == "postgresql+psycopg2://u:p@host/db?sslmode=require"
    )
