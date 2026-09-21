"""Environment bootstrap. Must run before anything under `src.` is imported.

pytest loads the rootdir conftest before collecting tests, which is the only window in
which this is guaranteed. Keep it to `os.environ` writes — importing `src.*` from here
would defeat the purpose, since several config modules resolve their settings on first use.

A dedicated `.env.test` is loaded if present, and nothing else is. The general dotenv files
are deliberately left alone: this suite TRUNCATEs between tests and drops the public schema
once per session, so a developer's `.env` pointing at the dev database would destroy it. The
guard in tests/conftest.py refuses to run against a database whose name isn't the test one.
"""
import os
from pathlib import Path

_ENV_TEST = Path(__file__).resolve().parent / ".env.test"
if _ENV_TEST.exists():
    from dotenv import load_dotenv

    load_dotenv(_ENV_TEST, override=True)

# `testing`, not `development` — this exercises the production branches of _secret_key(),
# cors_allowed_origins(), refresh_cookie_flags() and the logger's console level, which are
# the ones that actually ship.
os.environ["FLASK_ENV"] = "testing"

os.environ["DATABASE_URL"] = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/studio3_test"
)
os.environ["REDIS_URL"] = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")

os.environ.setdefault("SECRET_KEY", "test-secret-key")
# 32+ bytes: PyJWT warns below that for HMAC-SHA256, and a warning per token signed
# buries real output.
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-key-at-least-32-bytes-long")
os.environ.setdefault("JWT_ACCESS_EXPIRY_MINUTES", "15")
os.environ.setdefault("FRONTEND_URL", "http://localhost:5173")

# Two comma-separated secrets on purpose: stripe_client.webhook_secrets() returns a list and
# construct_event loops over it (one endpoint for the platform account, one for Connect).
# A single secret would leave that loop uncovered.
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_primary,whsec_test_connect")
os.environ.setdefault("PLATFORM_CURRENCY", "usd")

# Commission rates are deliberately NOT pinned here. Pinning them duplicated the
# product default in the harness, and when the real rate moved to 20% the tests went on
# asserting the harness's stale 10%. Cleared rather than left alone so a stray shell
# export cannot change what the suite measures either.
for _rate in ("PLATFORM_COMMISSION_BPS", "PLATFORM_TICKET_COMMISSION_BPS"):
    os.environ.pop(_rate, None)

# Default to Stripe "not configured" so tests must opt in via the `stripe_enabled` fixture.
# This keeps the dev-mode branches honest and stops a stray real key from leaking in.
os.environ.pop("STRIPE_SECRET_KEY", None)

# Keep S3/SES/Firebase unconfigured — every one of them is fail-open by design and the
# tests should exercise that, not a half-configured client.
for _optional in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "S3_BUCKET",
                  "S3_PUBLIC_BASE_URL", "SES_FROM_EMAIL",
                  "FIREBASE_SERVICE_ACCOUNT_PATH", "FIREBASE_SERVICE_ACCOUNT_JSON",
                  "SENTRY_DSN"):
    os.environ.pop(_optional, None)
