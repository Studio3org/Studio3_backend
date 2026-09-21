"""Stripe Connect onboarding, on the Accounts v2 API.

Artists get the Express dashboard so Stripe hosts KYC/identity collection (the platform never
touches bank or ID data, per NFR-5) while the platform keeps control of the experience.

Accounts are created with the `recipient` configuration only. The platform is the merchant of
record — buyers pay us, and we transfer the artist's share on — so an artist never needs to
accept a charge. `stripe_balance.stripe_transfers` is the v2 name for what v1 called the
`transfers` capability, and is the one required for indirect charges.

Only account creation and the onboarding link use /v2. Stripe accepts a v2 account id on the
v1 endpoints and answers in the v1 shape, so reads, transfers and dashboard links stay where
they were.
"""
import uuid
from datetime import datetime

from flask import g

from src.shared.config.database import SessionLocal
from src.shared.config.stripe_client import (
    connect_return_urls,
    get_stripe,
    get_stripe_v2,
    payouts_ready,
    platform_currency,
    stripe_configured,
)
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.user.user_dao import get_user_by_id

logger = get_logger(__name__)


def _account_country() -> str:
    import os
    return (os.getenv("CONNECT_ACCOUNT_COUNTRY") or "US").strip().upper()


def _unix(expires_at) -> int | None:
    """Account Link expiry as a Unix timestamp, whichever shape Stripe sent.

    v1 returned an integer; v2 returns RFC 3339 ("2026-09-21T09:34:10.000Z"). The mobile app
    parses an integer, so the conversion happens here rather than in every client.
    """
    if expires_at is None or isinstance(expires_at, int):
        return expires_at
    try:
        return int(datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")).timestamp())
    except ValueError:
        logger.warning("Unparseable Account Link expiry %r; omitting.", expires_at)
        return None



def start_onboarding():
    """Create the artist's Express account if needed and return a fresh onboarding link.

    Account Links are single-use and expire within minutes, so a new one is minted on every
    call rather than cached.
    """
    if not stripe_configured():
        raise AppError("Payouts are not configured yet.", 503)

    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        if not user:
            raise AppError("User not found.", 404)

        client = get_stripe_v2()
        if not user.stripe_account_id:
            account = client.v2.core.accounts.create(
                {
                    "contact_email": user.email,
                    "display_name": user.username,
                    # Express requires the platform to own fees and losses. That is what v1
                    # `type="express"` already meant, so liability is unchanged.
                    "dashboard": "express",
                    "identity": {
                        # v2 wants the country lowercased; the v1 read returns it uppercase.
                        "country": _account_country().lower(),
                        "entity_type": "individual",
                    },
                    "configuration": {
                        "recipient": {
                            "capabilities": {
                                "stripe_balance": {"stripe_transfers": {"requested": True}}
                            }
                        }
                    },
                    "defaults": {
                        "currency": platform_currency(),
                        "responsibilities": {
                            "fees_collector": "application",
                            "losses_collector": "application",
                        },
                    },
                    "include": ["configuration.recipient"],
                },
                # In v2 the idempotency key is a request option, not a parameter. Same key as
                # before, so a double tap still cannot create two accounts for one artist.
                options={"idempotency_key": f"connect_account:{user.id}"},
            )
            user.stripe_account_id = account.id
            db.commit()
            logger.info("Created Connect account %s for user %s.", account.id, user.id)

        return_url, refresh_url = connect_return_urls()
        link = client.v2.core.account_links.create(
            {
                "account": user.stripe_account_id,
                "use_case": {
                    "type": "account_onboarding",
                    "account_onboarding": {
                        "configurations": ["recipient"],
                        "return_url": return_url,
                        "refresh_url": refresh_url,
                    },
                },
            }
        )
        return {
            "onboardingUrl": link.url,
            "expiresAt": _unix(link.expires_at),
            "stripeAccountId": user.stripe_account_id,
        }, 200
    finally:
        db.close()


def onboarding_status():
    """Current Connect state for the signed-in artist.

    Reads live from Stripe and syncs the local flag, but the account.updated webhook is the
    real source of truth — this exists so the app can show what's still outstanding.
    """
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        if not user:
            raise AppError("User not found.", 404)

        if not user.stripe_account_id:
            return {
                "onboarded": False,
                "payoutsEnabled": False,
                "chargesEnabled": False,
                "stripeAccountId": None,
                "requirementsDue": [],
                "disabledReason": None,
                "canListForSale": False,
            }, 200

        if not stripe_configured():
            return {
                "onboarded": True,
                "payoutsEnabled": user.stripe_payouts_enabled,
                "chargesEnabled": user.stripe_payouts_enabled,
                "stripeAccountId": user.stripe_account_id,
                "requirementsDue": [],
                "disabledReason": None,
                "canListForSale": user.stripe_payouts_enabled,
            }, 200

        # v1 retrieve, deliberately: Stripe answers for a v2 account in the v1 shape, so
        # this and the account.updated handler read the same fields as before.
        stripe = get_stripe()
        account = stripe.Account.retrieve(user.stripe_account_id)
        payouts_enabled = bool(account.get("payouts_enabled"))
        charges_enabled = bool(account.get("charges_enabled"))
        enabled = payouts_ready(account)

        if user.stripe_payouts_enabled != enabled:
            user.stripe_payouts_enabled = enabled
            db.commit()

        requirements = account.get("requirements") or {}
        return {
            "onboarded": bool(account.get("details_submitted")),
            "payoutsEnabled": payouts_enabled,
            "chargesEnabled": charges_enabled,
            "stripeAccountId": user.stripe_account_id,
            "requirementsDue": list(requirements.get("currently_due") or []),
            "disabledReason": requirements.get("disabled_reason"),
            "canListForSale": enabled,
        }, 200
    finally:
        db.close()


def dashboard_link():
    """Short-lived Express dashboard link so an artist can see their own payouts."""
    if not stripe_configured():
        raise AppError("Payouts are not configured yet.", 503)
    db = SessionLocal()
    try:
        user = get_user_by_id(db, uuid.UUID(g.user["id"]))
        if not user or not user.stripe_account_id:
            raise AppError("Complete payout onboarding first.", 409)
        stripe = get_stripe()
        link = stripe.Account.create_login_link(user.stripe_account_id)
        return {"url": link["url"]}, 200
    finally:
        db.close()
