"""Every environment variable this service reads, declared once.

Two things this buys that scattered `os.getenv` calls do not:

1. **A boot-time check that names every problem at once.** A misconfigured deploy used to be
   a guessing game against the build queue — fix one variable, redeploy, discover the next.
2. **A contract the tests can enforce.** tests/unit/test_config_contract.py asserts that
   every variable the code reads is declared here, that .env.example documents all of them,
   and that render.yaml declares the production-required ones. Drift becomes a failing test
   rather than a discovery during an incident.

Adding a variable means adding it here. The contract test fails otherwise.
"""
import os
from dataclasses import dataclass, field

from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

DEVELOPMENT = "development"
TESTING = "testing"
# A deployed environment that is not production. Behaves like production for anything
# security-shaped (secure cookies, no file logging, real CORS), but does not require the
# payment credentials, so it can exercise the dev-mode checkout path. It does need storage.
STAGING = "staging"
PRODUCTION = "production"

# Environments that are deployed rather than run on someone's laptop.
DEPLOYED = (STAGING, PRODUCTION)


@dataclass(frozen=True)
class EnvVar:
    name: str
    description: str
    # Environments where a missing value is fatal at boot.
    required_in: frozenset = field(default_factory=frozenset)
    default: str | None = None
    # True = never log the value, not even truncated.
    secret: bool = False
    # Alternative names the code also accepts (see s3_client._env).
    aliases: tuple = ()


# Infrastructure and secrets: a deployed environment cannot function without these.
_REQUIRED_DEPLOYED = frozenset({STAGING, PRODUCTION})
# Payments: production only. Staging deliberately runs without them so the dev-mode
# checkout path stays exercisable. Storage is not in this group: the local media fallback
# is a development-only route, so a deployed environment without S3 cannot store media.
_REQUIRED_IN_PROD = frozenset({PRODUCTION})

ENV_VARS: tuple[EnvVar, ...] = (
    # --- core ---------------------------------------------------------------------------
    EnvVar("FLASK_ENV", "Which environment this process is running as.", default=DEVELOPMENT),
    EnvVar("PORT", "Port for the dev server (run.py). Render supplies its own.", default="9000"),
    EnvVar("DATABASE_URL", "PostgreSQL connection string.", _REQUIRED_DEPLOYED, secret=True),
    EnvVar("REDIS_URL", "Redis connection string — sessions, OTP, Socket.IO pub/sub.",
           _REQUIRED_DEPLOYED, secret=True),

    # --- auth ---------------------------------------------------------------------------
    EnvVar("SECRET_KEY", "Signs the admin session cookie, which gates refunds and payouts.",
           _REQUIRED_DEPLOYED, secret=True),
    EnvVar("JWT_SECRET", "Signs API access tokens.", _REQUIRED_DEPLOYED, secret=True),
    EnvVar("JWT_ACCESS_EXPIRY_MINUTES", "Access token lifetime.", default="15"),
    EnvVar("SALT_ROUNDS", "bcrypt cost factor for password hashing.", default="10"),

    # --- web ----------------------------------------------------------------------------
    EnvVar("FRONTEND_URL", "Canonical web origin. Always CORS-allowed; used in share links.",
           _REQUIRED_DEPLOYED),
    EnvVar("CORS_ORIGINS", "Extra browser origins, comma-separated."),
    EnvVar("BACKEND_URL", "This service's own public URL, for links it generates."),
    EnvVar("TRUSTED_PROXY_HOPS",
           "Reverse proxies in front of the app (nginx = 1). The client IP is read from that "
           "many X-Forwarded-For entries; 0 when nothing is in front.", default="1"),

    # --- app association -----------------------------------------------------------------
    # These make a shared https:// link open the app instead of a browser. Unset simply means
    # association is off — the custom studio3:// scheme still works and needs no verification
    # — so none of them are required at boot.
    EnvVar("IOS_TEAM_ID", "Apple Developer Team ID, prefixes the app id in the AASA file."),
    EnvVar("IOS_BUNDLE_ID", "iOS bundle identifier.", default="com.studio3.discover"),
    EnvVar("ANDROID_PACKAGE", "Android application id.", default="com.studio3.discover"),
    EnvVar("ANDROID_CERT_FINGERPRINTS",
           "SHA-256 signing fingerprints, comma-separated: release, debug, and Play's own "
           "re-signing key once the app is on Play."),

    # --- payments -----------------------------------------------------------------------
    # Without the webhook secret every webhook raises, Stripe retries then gives up, and paid
    # orders are never marked paid — a total revenue failure behind a healthy-looking service.
    # That is why it is required rather than fail-open like the other integrations.
    EnvVar("STRIPE_SECRET_KEY", "Stripe API key. Unset = dev mode, checkout auto-confirms.",
           _REQUIRED_IN_PROD, secret=True),
    EnvVar("STRIPE_WEBHOOK_SECRET",
           "Webhook signing secret(s), comma-separated: platform endpoint, Connect endpoint.",
           _REQUIRED_IN_PROD, secret=True),
    EnvVar("PLATFORM_CURRENCY", "ISO currency for charges and payouts.", default="usd"),
    EnvVar("PLATFORM_COMMISSION_BPS",
           "Commission on original art, in basis points (2000 = 20%).", default="2000"),
    EnvVar("PLATFORM_TICKET_COMMISSION_BPS",
           "Commission on event tickets, in basis points (800 = 8%).", default="800"),
    EnvVar("SALES_TAX_RATE_BPS",
           "Flat sales tax on art and tickets, in basis points (825 = 8.25%).", default="825"),
    # --- shipping (Phase 8, Convelio) -------------------------------------------------------
    EnvVar("CONVELIO_ENABLED",
           "Convelio for every shipment (on by default; needs CONVELIO_API_KEY). false = the "
           "flat-rate fallback.",
           default="true"),
    EnvVar("CONVELIO_API_KEY", "Convelio API key (sk_test_ sandbox / sk_live_ production). Also "
           "verifies their webhook signatures.", secret=True),
    EnvVar("CONVELIO_BASE_URL", "Override the Convelio API URL (defaults from the key's prefix)."),
    EnvVar("SHIPPING_QUOTE_VALID_HOURS", "Hours a shipping quote can be bought on.", default="24"),
    EnvVar("SHIPPING_INSURANCE_DEFAULT", "Shipping insurance pre-ticked at checkout.", default="true"),
    EnvVar("ORDER_AUTO_COMPLETE_DAYS",
           "Days after delivery before an unconfirmed order completes and the artist is paid.",
           default="7"),
    EnvVar("SALES_TAX_PROVIDER", "flat (SALES_TAX_RATE_BPS on the artwork) or stripe (Stripe Tax "
           "by delivery address).", default="flat"),
    EnvVar("AUCTION_PAYMENT_FIX_HOURS",
           "Hours an auction winner has to fix a card that couldn't be charged (Phase 7a).",
           default="12"),
    EnvVar("AUCTION_SECOND_CHANCE_HOURS",
           "Hours the next bidder has to accept a second-chance offer (Phase 7a).", default="24"),
    EnvVar("EVENT_PAYOUT_DELAY_HOURS",
           "Hours after an event ends before the host is paid for its tickets (Phase 4).",
           default="24"),
    EnvVar("TICKET_SIGNING_KEY",
           "Base64 32-byte Ed25519 seed that signs ticket QR codes. Derived from SECRET_KEY "
           "when unset; set it in production so rotating SECRET_KEY keeps tickets valid.",
           secret=True),
    # --- wallet passes (Phase 3A) -----------------------------------------------------------
    EnvVar("GOOGLE_WALLET_ISSUER_ID", "Google Pay & Wallet Console issuer id. Unset = no "
           "Google Wallet passes."),
    EnvVar("GOOGLE_WALLET_SERVICE_ACCOUNT_JSON", "Service account key (JSON text) with access "
           "to the Wallet issuer; signs the Add to Google Wallet links.", secret=True),
    EnvVar("APPLE_PASS_TYPE_ID", "Pass Type ID, e.g. pass.co.studio-3.ticket. Unset = no Apple "
           "Wallet passes."),
    EnvVar("APPLE_TEAM_ID", "Apple Developer team id the Pass Type ID belongs to."),
    EnvVar("APPLE_PASS_CERT_P12", "The Pass Type ID certificate and key, as base64 of the .p12.",
           secret=True),
    EnvVar("APPLE_PASS_CERT_PASSWORD", "Password of that .p12 (empty if none).", secret=True),
    EnvVar("APPLE_WWDR_CERT", "Apple's WWDR G4 intermediate certificate, base64 of the .cer."),
    EnvVar("CONNECT_ACCOUNT_COUNTRY", "Country for new Stripe Connect Express accounts.",
           default="US"),
    EnvVar("CONNECT_ONBOARDING_BASE_URL", "Base for Connect return/refresh redirects.",
           default="https://studio-3.co/connect"),

    # --- storage ------------------------------------------------------------------------
    EnvVar("AWS_ACCESS_KEY_ID", "AWS credentials for S3 and SES.", _REQUIRED_DEPLOYED, secret=True),
    EnvVar("AWS_SECRET_ACCESS_KEY", "AWS credentials for S3 and SES.", _REQUIRED_DEPLOYED,
           secret=True),
    EnvVar("AWS_REGION", "AWS region for S3 and SES.", default="us-east-1"),
    EnvVar("S3_BUCKET", "Media bucket.", _REQUIRED_DEPLOYED, aliases=("STUDIO3_S3_BUCKET",)),
    EnvVar("S3_PUBLIC_BASE_URL", "Public base URL media is served from.", _REQUIRED_DEPLOYED,
           aliases=("STUDIO3_S3_PUBLIC_BASE_URL",)),
    EnvVar("LOCAL_MEDIA_DIR", "Where /api/media/local writes when S3 is unconfigured (development only)."),

    # --- notifications ------------------------------------------------------------------
    EnvVar("SES_FROM_EMAIL", "Verified SES sender. Unset = OTP and reset emails are skipped."),
    EnvVar("FIREBASE_SERVICE_ACCOUNT_PATH", "Path to the FCM service account JSON."),
    EnvVar("FIREBASE_SERVICE_ACCOUNT_JSON", "Inline FCM service account JSON.", secret=True),

    # --- share links --------------------------------------------------------------------
    EnvVar("APP_STORE_URL", "iOS listing, for the 'get the app' interstitial."),
    EnvVar("PLAY_STORE_URL", "Android listing, for the 'get the app' interstitial."),

    # --- background jobs ------------------------------------------------------------------
    EnvVar("CELERY_BROKER_URL",
           "Overrides the broker URL. Unset = REDIS_URL with CELERY_REDIS_DB as its index.",
           secret=True),
    EnvVar("CELERY_REDIS_DB",
           "Redis database index for the job queue. Kept off the cache index so an eviction "
           "policy cannot drop queued jobs.",
           default="1"),

    # --- observability ------------------------------------------------------------------
    EnvVar("SENTRY_DSN", "Error tracking. Unset = Sentry is not initialised at all.",
           secret=True),
)

ENV_VARS_BY_NAME = {var.name: var for var in ENV_VARS}

# Every name the code may legitimately read, aliases included. The contract test compares
# what it finds in the source against this.
ALL_ENV_NAMES = frozenset(
    {var.name for var in ENV_VARS} | {alias for var in ENV_VARS for alias in var.aliases}
)


def _is_set(var: EnvVar) -> bool:
    return any((os.getenv(name) or "").strip() for name in (var.name, *var.aliases))


def missing_required(env: str) -> list[str]:
    """Names of every required-but-unset variable for `env`. Empty list means ok."""
    return [var.name for var in ENV_VARS if env in var.required_in and not _is_set(var)]


def unset_optional(env: str) -> list[str]:
    """Optional variables with no value — each disables a feature, none are fatal."""
    return [
        var.name
        for var in ENV_VARS
        if env not in var.required_in and var.default is None and not _is_set(var)
    ]


def require_environment(env: str) -> None:
    """Fail at boot if anything required is missing, naming all of it in one message.

    Refusing to start beats starting subtly broken: an unconfigured webhook secret produces
    a service that answers 200 on its health check while silently never marking an order
    paid, which is far harder to notice than a deploy that stops.
    """
    missing = missing_required(env)
    if missing:
        raise RuntimeError(
            f"Missing required environment variables for {env}: {', '.join(sorted(missing))}. "
            "See .env.example for what each one is."
        )


def warn_unset_optional(env: str) -> None:
    """One log line listing the features that are switched off. Not an error."""
    unset = unset_optional(env)
    if unset:
        logger.warning(
            "Optional configuration not set (%d): %s. Those features are disabled.",
            len(unset), ", ".join(sorted(unset)),
        )
