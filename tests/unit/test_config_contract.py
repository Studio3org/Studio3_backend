"""Keeps settings.py, .env.example and render.yaml in sync without anyone remembering to.

Configuration drift is invisible until a deploy: .env.example was missing eight variables
the code read — including STRIPE_WEBHOOK_SECRET, whose absence silently breaks every
payment — and render.yaml declared none of the Stripe or platform settings at all. These
tests turn that class of mistake into a failing build.
"""
import ast
import re
from pathlib import Path

import pytest

from src.shared.config.settings import (
    ALL_ENV_NAMES,
    DEPLOYED,
    ENV_VARS,
    PRODUCTION,
    STAGING,
    missing_required,
    require_environment,
)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SOURCE_ROOTS = ("src", "run.py", "wsgi.py", "alembic/env.py")

# Reads that are not configuration: Flask's own config dict, and names set by the host.
NOT_CONFIGURATION = {"TESTING", "WTF_CSRF_CHECK_DEFAULT", "RENDER_GIT_COMMIT"}


def _python_files():
    for root in SOURCE_ROOTS:
        path = BASE_DIR / root
        if path.is_dir():
            yield from (p for p in path.rglob("*.py") if "__pycache__" not in str(p))
        elif path.exists():
            yield path


def _is_os_attr(node: ast.AST, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _env_names_read_by(path: Path) -> set[str]:
    """Every environment variable name this file reads.

    Matches precisely — os.getenv, os.environ.get, os.environ[...] and the project's own
    `_env()` helper in s3_client. A looser match on any `.get("SOMETHING")` picks up default
    values like body.get("currency", "USD") and reports them as undeclared config.

    The `_env` helper takes alias names as extra positional arguments, so all of its string
    args count; for os.getenv only the first does, the second being the default.
    """
    names: set[str] = set()
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            take_all = isinstance(func, ast.Name) and func.id == "_env"
            take_first = _is_os_attr(func, "getenv") or (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and _is_os_attr(func.value, "environ")
            )
            if not (take_all or take_first):
                continue
            args = node.args if take_all else node.args[:1]
            for arg in args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.add(arg.value)
        elif isinstance(node, ast.Subscript) and _is_os_attr(node.value, "environ"):
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
    return names - NOT_CONFIGURATION


def test_every_env_var_the_code_reads_is_declared():
    undeclared = {}
    for path in _python_files():
        for name in _env_names_read_by(path) - ALL_ENV_NAMES:
            undeclared.setdefault(name, []).append(str(path.relative_to(BASE_DIR)))
    assert not undeclared, (
        "These environment variables are read but not declared in "
        f"src/shared/config/settings.py: {undeclared}"
    )


def test_env_example_documents_every_declared_var():
    text = (BASE_DIR / ".env.example").read_text()
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))
    declared = {var.name for var in ENV_VARS}

    assert not declared - documented, f".env.example is missing: {sorted(declared - documented)}"
    assert not documented - declared, (
        f".env.example documents variables nothing reads: {sorted(documented - declared)}"
    )


def _render_declared() -> set[str]:
    text = (BASE_DIR / "render.yaml").read_text()
    return set(re.findall(r"^\s*-\s*key:\s*([A-Z][A-Z0-9_]*)", text, re.MULTILINE))


def _render_flask_env() -> str:
    text = (BASE_DIR / "render.yaml").read_text()
    match = re.search(r"-\s*key:\s*FLASK_ENV\s*\n\s*value:\s*(\S+)", text)
    assert match, "render.yaml must set FLASK_ENV explicitly"
    return match.group(1)


def test_render_yaml_declares_everything_its_environment_requires():
    """render.yaml deploys the staging service, so it is staging's required set that must be
    covered — not production's, which has no deploy config in this repo yet."""
    env = _render_flask_env()
    required = {var.name for var in ENV_VARS if env in var.required_in}

    missing = required - _render_declared()
    assert not missing, (
        f"render.yaml deploys FLASK_ENV={env} but does not declare: {sorted(missing)}. "
        "The service would refuse to boot."
    )
    unknown = _render_declared() - {var.name for var in ENV_VARS}
    assert not unknown, f"render.yaml declares unknown variables: {sorted(unknown)}"


def test_render_yaml_deploys_a_known_environment():
    assert _render_flask_env() in ("development", STAGING, PRODUCTION)


def test_staging_requires_infrastructure_but_not_payment_credentials():
    """Staging is a test environment: it must have a database, Redis and real secrets, but
    deliberately runs without Stripe and S3 so the unconfigured paths stay exercisable."""
    staging_required = {var.name for var in ENV_VARS if STAGING in var.required_in}
    assert {"DATABASE_URL", "REDIS_URL", "SECRET_KEY", "JWT_SECRET"} <= staging_required
    assert not staging_required & {"STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "S3_BUCKET"}


def test_every_deployed_environment_requires_the_signing_secrets():
    """SECRET_KEY signs the admin session cookie, which gates refunds and payout releases; a
    default value on any internet-facing box would let anyone forge one."""
    for env in DEPLOYED:
        required = {var.name for var in ENV_VARS if env in var.required_in}
        assert {"SECRET_KEY", "JWT_SECRET"} <= required, env


def test_render_yaml_health_check_points_at_liveness_not_readiness():
    """/health touches the database and Redis; pointing Render's restart trigger at it would
    turn a transient outage into a restart loop."""
    text = (BASE_DIR / "render.yaml").read_text()
    match = re.search(r"^\s*healthCheckPath:\s*(\S+)", text, re.MULTILINE)
    assert match, "render.yaml should declare healthCheckPath explicitly"
    assert match.group(1) == "/"


def test_production_requires_the_stripe_webhook_secret():
    """Singled out because its absence is the most dangerous: every webhook fails signature
    verification, Stripe gives up retrying, and orders are paid at Stripe but never here —
    behind a health check that still reports ok."""
    required = {var.name for var in ENV_VARS if PRODUCTION in var.required_in}
    assert "STRIPE_WEBHOOK_SECRET" in required


def test_require_environment_reports_every_missing_var_at_once(monkeypatch):
    """One message, not a five-round guessing game against the deploy queue."""
    for var in ENV_VARS:
        monkeypatch.delenv(var.name, raising=False)
        for alias in var.aliases:
            monkeypatch.delenv(alias, raising=False)

    missing = missing_required(PRODUCTION)
    assert len(missing) > 5

    with pytest.raises(RuntimeError) as exc:
        require_environment(PRODUCTION)
    for name in missing:
        assert name in str(exc.value)


def test_testing_environment_needs_nothing_extra():
    """conftest sets what the suite needs; if this fails the harness and registry disagree."""
    assert missing_required("testing") == []


def test_aliases_satisfy_a_requirement(monkeypatch):
    """s3_client accepts STUDIO3_S3_BUCKET as well as S3_BUCKET, so the check must too."""
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setenv("STUDIO3_S3_BUCKET", "some-bucket")
    assert "S3_BUCKET" not in missing_required(PRODUCTION)
