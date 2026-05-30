"""Stripe subscriptions, tier definitions, and transaction-limit enforcement.

Tiers are defined in code; Stripe price IDs come from env vars so the user can
plug in real test/live keys later. If Stripe is not configured, checkout
endpoints return a 503 and the UI shows a friendly notice.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session

from . import email_service
from .database import Base, Transaction, User, engine

try:
    import stripe  # type: ignore
except ImportError:  # pragma: no cover
    stripe = None  # type: ignore


# ---------------------------------------------------------------------------
# Tier catalogue
# ---------------------------------------------------------------------------

TIERS: dict[str, dict] = {
    "free": {
        "key": "free",
        "name": "Free",
        "price_monthly": 0.0,
        "price_display": "$0",
        "price_yearly": 0.0,
        "price_yearly_display": "$0",
        "yearly_savings_pct": 0,
        "transactions_per_year": 25,
        "tagline": "Try CardTax with a small portfolio.",
        "features": [
            "25 transactions per year",
            "Federal + state + local tax estimates",
            "Schedule D / Form 8949 preview",
            "CSV import (eBay, Whatnot)",
            "Hobby / Investor / Dealer quiz",
        ],
        "not_included": ["AI card scanner (coming soon)", "Priority support"],
        "cta": "Current plan",
        "price_env": None,  # no Stripe price
    },
    "starter": {
        "key": "starter",
        "name": "Starter",
        "price_monthly": 4.99,
        "price_display": "$4.99",
        "price_yearly": 49.99,
        "price_yearly_display": "$49.99",
        "yearly_savings_pct": 17,
        "transactions_per_year": 200,
        "tagline": "For weekend sellers and casual collectors.",
        "features": [
            "200 transactions per year",
            "Everything in Free",
            "Cost-basis components (grading, shipping, etc.)",
            "Quarterly estimated tax tracker",
        ],
        "not_included": ["AI card scanner (coming soon)"],
        "cta": "Upgrade to Starter",
        "price_env": "STRIPE_PRICE_STARTER",
    },
    "pro": {
        "key": "pro",
        "name": "Pro",
        "price_monthly": 9.99,
        "price_display": "$9.99",
        "price_yearly": 99.99,
        "price_yearly_display": "$99.99",
        "yearly_savings_pct": 17,
        "transactions_per_year": 1000,
        "tagline": "For active resellers and investors.",
        "features": [
            "1,000 transactions per year",
            "Everything in Starter",
            "Loss-harvesting suggestions",
            "Multi-state breakdown",
            "Priority support",
        ],
        "not_included": [],
        "cta": "Upgrade to Pro",
        "price_env": "STRIPE_PRICE_PRO",
        "badge": "Most popular",
    },
    "unlimited": {
        "key": "unlimited",
        "name": "Unlimited",
        "price_monthly": 19.99,
        "price_display": "$19.99",
        "price_yearly": 199.99,
        "price_yearly_display": "$199.99",
        "yearly_savings_pct": 17,
        "transactions_per_year": None,  # unlimited
        "tagline": "For dealers and full-time sellers.",
        "features": [
            "Unlimited transactions",
            "Everything in Pro",
            "AI card scanner (coming soon)",
            "Form 8949 CSV export",
            "Schedule C dealer support",
            "Concierge onboarding",
        ],
        "not_included": [],
        "cta": "Go Unlimited",
        "price_env": "STRIPE_PRICE_UNLIMITED",
    },
}

TIER_ORDER = ["free", "starter", "pro", "unlimited"]

# Features that require Unlimited
PREMIUM_FEATURES = {"ai_scanner", "form_8949_export"}


# ---------------------------------------------------------------------------
# Stripe config
# ---------------------------------------------------------------------------

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get(
    "STRIPE_PUBLISHABLE_KEY", "pk_test_REPLACE_ME"
)
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")

if stripe is not None and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def stripe_configured() -> bool:
    return bool(stripe is not None and STRIPE_SECRET_KEY)


def price_id_for(tier: str) -> Optional[str]:
    info = TIERS.get(tier)
    if not info or not info.get("price_env"):
        return None
    return os.environ.get(info["price_env"]) or None


# ---------------------------------------------------------------------------
# Tier helpers
# ---------------------------------------------------------------------------


def tier_info(tier: str) -> dict:
    return TIERS.get(tier or "free", TIERS["free"])


def yearly_limit(tier: str) -> Optional[int]:
    return tier_info(tier).get("transactions_per_year")


def transaction_count_for_year(db: Session, user_id: int, year: Optional[int] = None) -> int:
    year = year or date.today().year
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    return (
        db.query(Transaction)
        .filter(
            Transaction.user_id == user_id,
            Transaction.sale_date >= start,
            Transaction.sale_date <= end,
            Transaction.deleted_at.is_(None),
        )
        .count()
    )


def usage_for(db: Session, user: User) -> dict:
    """Snapshot of usage for the account UI."""
    year = date.today().year
    used = transaction_count_for_year(db, user.id, year)
    limit = yearly_limit(user.subscription_tier or "free")
    info = tier_info(user.subscription_tier or "free")
    return {
        "tier": user.subscription_tier or "free",
        "tier_name": info["name"],
        "price_display": info["price_display"],
        "year": year,
        "used": used,
        "limit": limit,  # None = unlimited
        "remaining": (None if limit is None else max(0, limit - used)),
        "percent": (None if limit is None else min(1.0, used / max(1, limit))),
        "subscription_status": user.subscription_status or "active",
        "current_period_end": (
            user.current_period_end.isoformat() if user.current_period_end else None
        ),
        "stripe_configured": stripe_configured(),
    }


def has_feature(user: User, feature: str) -> bool:
    """Premium features are gated to Unlimited."""
    if feature not in PREMIUM_FEATURES:
        return True
    return (user.subscription_tier or "free") == "unlimited"


def enforce_transaction_limit(db: Session, user: User, additional: int = 1) -> None:
    """Raise 402 if adding `additional` more transactions exceeds the user's tier limit.

    The limit is on sales in the *current calendar year* (sale_date-based) since that's
    the natural unit for a tax-year tool.
    """
    limit = yearly_limit(user.subscription_tier or "free")
    if limit is None:
        return
    used = transaction_count_for_year(db, user.id)
    if used + additional > limit:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "limit_reached",
                "tier": user.subscription_tier or "free",
                "limit": limit,
                "used": used,
                "would_add": additional,
                "message": (
                    f"You've used {used} of {limit} transactions on your "
                    f"{tier_info(user.subscription_tier or 'free')['name']} plan. "
                    "Upgrade to add more."
                ),
            },
        )


# ---------------------------------------------------------------------------
# Stripe checkout / portal
# ---------------------------------------------------------------------------


def _ensure_stripe_customer(user: User, db: Session) -> str:
    """Create a Stripe customer for this user if needed; return customer id."""
    if not stripe_configured():
        raise HTTPException(503, "Stripe is not configured. Set STRIPE_SECRET_KEY.")
    if user.stripe_customer_id:
        return user.stripe_customer_id
    customer = stripe.Customer.create(
        email=user.email,
        name=user.display_name or user.email,
        metadata={"cardtax_user_id": str(user.id)},
    )
    user.stripe_customer_id = customer["id"]
    db.commit()
    return customer["id"]


def create_checkout_session(db: Session, user: User, tier: str) -> str:
    if tier not in TIERS or tier == "free":
        raise HTTPException(400, "Unknown plan")
    if not stripe_configured():
        raise HTTPException(503, "Stripe is not configured. Set STRIPE_SECRET_KEY.")
    price_id = price_id_for(tier)
    if not price_id:
        raise HTTPException(
            503,
            f"Stripe price ID is not configured. Set {TIERS[tier]['price_env']} in env.",
        )
    customer_id = _ensure_stripe_customer(user, db)
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{APP_BASE_URL}/account?checkout=success",
        cancel_url=f"{APP_BASE_URL}/pricing?checkout=cancelled",
        allow_promotion_codes=True,
        metadata={"cardtax_user_id": str(user.id), "tier": tier},
        subscription_data={"metadata": {"cardtax_user_id": str(user.id), "tier": tier}},
    )
    return session.url


def create_billing_portal(user: User, db: Session) -> str:
    if not stripe_configured():
        raise HTTPException(503, "Stripe is not configured.")
    if not user.stripe_customer_id:
        raise HTTPException(400, "No billing account yet — subscribe to a plan first.")
    session = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=f"{APP_BASE_URL}/account",
    )
    return session.url


# ---------------------------------------------------------------------------
# Webhook handling
# ---------------------------------------------------------------------------


def _tier_from_price_id(price_id: str) -> Optional[str]:
    for key, info in TIERS.items():
        env = info.get("price_env")
        if env and os.environ.get(env) == price_id:
            return key
    return None


def _apply_subscription_to_user(user: User, sub: dict) -> None:
    """Mirror a Stripe subscription object onto our User row."""
    user.stripe_subscription_id = sub.get("id")
    status = sub.get("status") or "active"
    user.subscription_status = status

    cpe = sub.get("current_period_end")
    if cpe:
        try:
            user.current_period_end = datetime.utcfromtimestamp(int(cpe))
        except (TypeError, ValueError):
            pass

    # Resolve tier from price id (prefer metadata if set)
    meta_tier = (sub.get("metadata") or {}).get("tier")
    if meta_tier in TIERS:
        user.subscription_tier = meta_tier
    else:
        items = (sub.get("items") or {}).get("data") or []
        if items:
            price_id = (items[0].get("price") or {}).get("id")
            t = _tier_from_price_id(price_id) if price_id else None
            if t:
                user.subscription_tier = t

    # Cancelled / expired -> free
    if status in ("canceled", "incomplete_expired", "unpaid"):
        user.subscription_tier = "free"


def handle_stripe_event(db: Session, event: dict) -> None:
    """Dispatch a verified Stripe event to user-state changes."""
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "checkout.session.completed":
        customer_id = obj.get("customer")
        sub_id = obj.get("subscription")
        if not customer_id or not sub_id:
            return
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
        if not user:
            meta_uid = (obj.get("metadata") or {}).get("cardtax_user_id")
            if meta_uid:
                user = db.query(User).filter(User.id == int(meta_uid)).first()
                if user and not user.stripe_customer_id:
                    user.stripe_customer_id = customer_id
        if not user:
            return
        sub = stripe.Subscription.retrieve(sub_id)
        _apply_subscription_to_user(user, sub)
        db.commit()
        if user.email and getattr(user, "email_pref_subscription", True):
            info = tier_info(user.subscription_tier or "free")
            try:
                email_service.send_subscription_confirmation(
                    user.email, info["name"], info["price_display"],
                )
            except Exception:
                pass
        return

    if etype.startswith("customer.subscription."):
        customer_id = obj.get("customer")
        if not customer_id:
            return
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
        if not user:
            return
        if etype == "customer.subscription.deleted":
            old_tier = user.subscription_tier or "free"
            user.subscription_tier = "free"
            user.subscription_status = "canceled"
            user.stripe_subscription_id = None
            user.current_period_end = None
            db.commit()
            if user.email and getattr(user, "email_pref_subscription", True):
                try:
                    email_service.send_subscription_cancellation(
                        user.email, tier_info(old_tier)["name"],
                    )
                except Exception:
                    pass
        else:
            _apply_subscription_to_user(user, obj)
            db.commit()
        return


# ---------------------------------------------------------------------------
# Webhook retry queue
# ---------------------------------------------------------------------------


class WebhookEvent(Base):
    """One row per incoming Stripe webhook, persisted before processing so a
    crash mid-handler doesn't lose the event.

    ``status``:
      * ``received``  — verified and stored, not yet processed
      * ``processed`` — handler ran successfully
      * ``failed``    — handler raised; retry on the next webhook (up to attempts cap)
      * ``abandoned`` — too many failed attempts, manual review needed
    """
    __tablename__ = "stripe_webhook_events"
    id = Column(Integer, primary_key=True)
    event_id = Column(String, unique=True, index=True)  # Stripe's evt_xxx id
    event_type = Column(String, default="", index=True)
    payload = Column(Text, default="")  # raw JSON for re-processing
    signature = Column(String, default="")
    status = Column(String, default="received", index=True)
    attempts = Column(Integer, default=0, nullable=False)
    last_error = Column(Text, default="")
    received_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)


def _ensure_webhook_table() -> None:
    """Create the table on first import. Cheap idempotent call — only creates
    tables that don't exist, won't touch existing ones."""
    Base.metadata.create_all(bind=engine, tables=[WebhookEvent.__table__])


_ensure_webhook_table()


def record_webhook(db: Session, event: dict, payload: bytes, sig: Optional[str]) -> WebhookEvent:
    """Persist an incoming webhook before handling it. If we've already seen
    this event_id, return the existing row so the handler can decide whether
    to re-process (idempotency)."""
    event_id = event.get("id") or ""
    existing = (
        db.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).first()
        if event_id else None
    )
    if existing:
        return existing
    rec = WebhookEvent(
        event_id=event_id,
        event_type=event.get("type") or "",
        payload=payload.decode("utf-8", errors="replace") if payload else "",
        signature=(sig or "")[:300],
        status="received",
        attempts=0,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def mark_webhook_processed(db: Session, rec: WebhookEvent) -> None:
    rec.status = "processed"
    rec.attempts = (rec.attempts or 0) + 1
    rec.processed_at = datetime.utcnow()
    db.commit()


def mark_webhook_failed(db: Session, rec: WebhookEvent, error: str) -> None:
    rec.status = "failed"
    rec.attempts = (rec.attempts or 0) + 1
    rec.last_error = (error or "")[:2000]
    db.commit()


def retry_failed_webhooks(
    db: Session,
    handler: Callable[[Session, dict], None],
    *,
    max_attempts: int = 5,
) -> int:
    """Re-run any failed webhooks whose attempts < max_attempts. Returns the
    number of events that succeeded on retry. Safe to call on every incoming
    webhook (rows past the cap are skipped)."""
    pending = (
        db.query(WebhookEvent)
        .filter(WebhookEvent.status == "failed")
        .filter(WebhookEvent.attempts < max_attempts)
        .order_by(WebhookEvent.received_at.asc())
        .limit(20)
        .all()
    )
    succeeded = 0
    for rec in pending:
        try:
            event = json.loads(rec.payload or "{}")
        except json.JSONDecodeError:
            rec.status = "abandoned"
            rec.last_error = "payload not valid JSON"
            db.commit()
            continue
        try:
            handler(db, event)
            mark_webhook_processed(db, rec)
            succeeded += 1
        except Exception as e:
            rec.attempts = (rec.attempts or 0) + 1
            rec.last_error = str(e)[:2000]
            if rec.attempts >= max_attempts:
                rec.status = "abandoned"
            db.commit()
    return succeeded


def verify_webhook(payload: bytes, sig_header: Optional[str]) -> dict:
    if not stripe_configured():
        raise HTTPException(503, "Stripe not configured")
    if not STRIPE_WEBHOOK_SECRET:
        # Without a signing secret we can't verify — refuse rather than trust
        # arbitrary bodies in production. In test/dev set STRIPE_WEBHOOK_SECRET.
        raise HTTPException(503, "STRIPE_WEBHOOK_SECRET not set; cannot verify webhook")
    try:
        return stripe.Webhook.construct_event(
            payload, sig_header or "", STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:  # SignatureVerificationError, ValueError, etc.
        raise HTTPException(400, f"Webhook signature failed: {e}")
