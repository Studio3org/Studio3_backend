"""Stripe Connect Express onboarding.

Express is chosen over Standard so Stripe hosts KYC/identity collection (the platform never
touches bank or ID data, per NFR-5) while the platform keeps control of the experience.
"""
import uuid

from flask import g, request

from src.shared.config.database import SessionLocal
from src.shared.config.stripe_client import (
    connect_return_urls,
    get_stripe,
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

        stripe = get_stripe()
        if not user.stripe_account_id:
            account = stripe.Account.create(
                type="express",
                email=user.email,
                country=_account_country(),
                default_currency=platform_currency(),
                capabilities={"transfers": {"requested": True}},
                business_type="individual",
                metadata={"user_id": str(user.id), "username": user.username},
                idempotency_key=f"connect_account:{user.id}",
            )
            user.stripe_account_id = account["id"]
            db.commit()
            logger.info("Created Connect account %s for user %s.", account["id"], user.id)

        return_url, refresh_url = connect_return_urls()
        link = stripe.AccountLink.create(
            account=user.stripe_account_id,
            refresh_url=refresh_url,
            return_url=return_url,
            type="account_onboarding",
        )
        return {
            "onboardingUrl": link["url"],
            "expiresAt": link["expires_at"],
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
                "canListForSale": False,
            }, 200

        if not stripe_configured():
            return {
                "onboarded": True,
                "payoutsEnabled": user.stripe_payouts_enabled,
                "chargesEnabled": user.stripe_payouts_enabled,
                "stripeAccountId": user.stripe_account_id,
                "requirementsDue": [],
                "canListForSale": user.stripe_payouts_enabled,
            }, 200

        stripe = get_stripe()
        account = stripe.Account.retrieve(user.stripe_account_id)
        payouts_enabled = bool(account.get("payouts_enabled"))
        charges_enabled = bool(account.get("charges_enabled"))
        enabled = payouts_enabled and charges_enabled

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
