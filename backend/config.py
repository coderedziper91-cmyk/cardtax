"""Environment-variable validation, gathered into one place.

Every other module reaches for ``os.environ`` directly today; that's fine for
local dev but means a missing production secret crashes the app at the moment
the feature is *used*, not at boot. ``Settings`` is loaded once on startup so
that mistake surfaces immediately.

Production detection is intentionally simple: if ``DATABASE_URL`` is set,
treat the deploy as production. (Local dev uses the SQLite fallback and never
sets it.) In that mode ``SECRET_KEY`` is required — there is no safe default
for a session-signing key in production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """Raised when required configuration is missing in production."""


def _get(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    # --- core ---
    secret_key: str
    database_url: str
    is_production: bool

    # --- public-facing URLs ---
    cardtax_public_url: str = ""
    app_base_url: str = "http://localhost:8000"

    # --- payments (Stripe) ---
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""

    # --- email ---
    sendgrid_api_key: str = ""
    email_from: str = "noreply@cardtax.app"
    email_from_name: str = "CardTax"

    # --- Google OAuth ---
    google_client_id: str = ""
    google_client_secret: str = ""

    # --- eBay OAuth ---
    ebay_client_id: str = ""
    ebay_client_secret: str = ""

    # --- third-party APIs ---
    openai_api_key: str = ""

    # --- analytics + admin ---
    google_analytics_id: str = ""
    cardtax_admin_token: str = ""

    # --- upload size limits (bytes) ---
    csv_max_bytes: int = 10 * 1024 * 1024  # 10 MB
    image_max_bytes: int = 5 * 1024 * 1024  # 5 MB

    # --- feature toggles surfaced as properties ---
    enabled: dict[str, bool] = field(default_factory=dict)


def load_settings() -> Settings:
    database_url = _get("DATABASE_URL")
    is_production = bool(database_url)

    # SECRET_KEY is the canonical name; CARDTAX_SESSION_SECRET is the legacy
    # name the rest of the codebase already reads. Accept either, prefer the
    # explicit SECRET_KEY when both are set.
    secret_key = _get("SECRET_KEY") or _get("CARDTAX_SESSION_SECRET")

    if is_production and not secret_key:
        raise ConfigError(
            "SECRET_KEY (or CARDTAX_SESSION_SECRET) must be set in production. "
            "Refusing to start with a randomly-generated key — that would "
            "invalidate every user's session on each restart."
        )

    if not secret_key:
        # Dev fallback — stable so local sessions persist across restarts.
        secret_key = "cardtax-dev-insecure-secret-change-me-in-production-32+chars"

    return Settings(
        secret_key=secret_key,
        database_url=database_url,
        is_production=is_production,
        cardtax_public_url=_get("CARDTAX_PUBLIC_URL"),
        app_base_url=_get("APP_BASE_URL", "http://localhost:8000"),
        stripe_secret_key=_get("STRIPE_SECRET_KEY"),
        stripe_publishable_key=_get("STRIPE_PUBLISHABLE_KEY"),
        stripe_webhook_secret=_get("STRIPE_WEBHOOK_SECRET"),
        sendgrid_api_key=_get("SENDGRID_API_KEY"),
        email_from=_get("CARDTAX_EMAIL_FROM", "noreply@cardtax.app"),
        email_from_name=_get("CARDTAX_EMAIL_FROM_NAME", "CardTax"),
        google_client_id=_get("GOOGLE_CLIENT_ID"),
        google_client_secret=_get("GOOGLE_CLIENT_SECRET"),
        ebay_client_id=_get("EBAY_CLIENT_ID"),
        ebay_client_secret=_get("EBAY_CLIENT_SECRET"),
        openai_api_key=_get("OPENAI_API_KEY"),
        google_analytics_id=_get("GOOGLE_ANALYTICS_ID"),
        cardtax_admin_token=_get("CARDTAX_ADMIN_TOKEN"),
    )


# Module-level singleton, evaluated at import time so configuration errors
# crash the worker before it starts serving traffic.
settings = load_settings()
