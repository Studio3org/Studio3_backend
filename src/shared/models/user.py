"""User model."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Boolean, Text, Float, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(30), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    password = Column(String(255), nullable=True)  # null for OAuth-only users
    image = Column(String(512), nullable=True)
    cover_photo_url = Column(String(512), nullable=True)
    bio = Column(Text, nullable=True)
    location = Column(String(255), nullable=True)
    phone = Column(String(32), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    email_verified = Column(Boolean, default=False, nullable=False)
    # "Primary interest" categorization (artist|collector|enthusiast) shown in onboarding —
    # NOT an authorization role. Admin access is is_admin below; never gate on this.
    role = Column(String(32), nullable=True)
    seller_enabled = Column(Boolean, default=False, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    # Stripe Connect (Express) — set once the artist starts onboarding. payouts_enabled is
    # mirrored from the account.updated webhook, not trusted from a client.
    stripe_account_id = Column(String(255), nullable=True, index=True)
    stripe_payouts_enabled = Column(Boolean, default=False, nullable=False)
    # The Stripe customer this user's saved cards belong to. Required for bidding: a hold is
    # re-authorised off-session weeks later with nobody present, and a payment method has to
    # belong to a customer to be reusable at all. Added by migration 031 but missed here, so
    # ensure_customer raised AttributeError on the first real bid — invisible until now only
    # because the hold tests run in dev mode, which skips Stripe entirely.
    stripe_customer_id = Column(String(255), nullable=True, unique=True)
    # Reduced commission for a specific artist, in basis points. Null = the standard
    # platform rate; set only for the client's named lower-rate tier.
    commission_bps_override = Column(Integer, nullable=True)
    onboarding_complete = Column(Boolean, default=False, nullable=False)
    taste_preferences = Column(JSONB, nullable=True)  # {mediums, styles, themes}
    last_username_change_at = Column(DateTime(timezone=True), nullable=True)
    pronouns = Column(String(50), nullable=True)
    website = Column(String(500), nullable=True)
    instagram = Column(String(100), nullable=True)
    twitter = Column(String(100), nullable=True)
    # Free-text "primary discipline" shown on the profile (e.g. "Digital Art") — UI-suggested
    # presets, not an enforced enum, same convention as pronouns above.
    category = Column(String(50), nullable=True)
    tags = Column(JSONB, nullable=True)  # list[str], up to 8 — discipline/style keywords
    # "Magnum opus" banner: a manually-pinned piece/post, or an auto-selection rule
    # computed at read time (no cron exists to materialize this — see user_serializers.py).
    banner_target_type = Column(String(16), nullable=True)  # piece|post
    banner_target_id = Column(UUID(as_uuid=True), nullable=True)
    banner_auto_rule = Column(String(16), default="none", nullable=False)  # most_saved|most_recent|none
    message_permission = Column(String(16), default="everyone", nullable=False)  # everyone|following|no_one
    profile_visibility = Column(String(16), default="public", nullable=False)  # public|private
    notification_preferences = Column(JSONB, nullable=True)  # {push: {...}, dailyDigest: {...}}
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    accounts = relationship("Account", back_populates="user", cascade="all, delete-orphan")
    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")
    refresh_tokens = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    password_reset_tokens = relationship(
        "PasswordResetToken", back_populates="user", cascade="all, delete-orphan"
    )
    username_history = relationship(
        "UsernameHistory", backref="user", cascade="all, delete-orphan"
    )
