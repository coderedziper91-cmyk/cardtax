"""Security hardening: CSRF, email verification, account lockout, admin flag.

This module adds tables (UserSecurity, LoginAttempt) without touching the
existing User schema. It also provides middleware and helpers for:

  * CSRF protection (per-session token, header + form field check)
  * Email verification (itsdangerous signed tokens, 24h TTL)
  * Account lockout after repeated failed logins
  * Password strength validation
  * Admin authentication (is_admin flag on UserSecurity, plus legacy token fallback)
  * Security response headers (CSP, HSTS, X-Frame-Options, etc.)
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Session, relationship
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from .database import Base, User, engine


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class UserSecurity(Base):
    """1:1 with User — all of the new auth state lives here so we don't touch
    the existing `users` schema."""
    __tablename__ = "user_security"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)

    email_verified = Column(Boolean, default=False, nullable=False)
    email_verified_at = Column(DateTime, nullable=True)

    failed_login_count = Column(Integer, default=0, nullable=False)
    last_failed_at = Column(DateTime, nullable=True)
    lockout_until = Column(DateTime, nullable=True)

    is_admin = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", backref="security", uselist=False)


class LoginAttempt(Base):
    """Per-email failed-login log. We use a small rolling window for the
    lockout decision rather than a single counter so a benign user who got
    locked out yesterday isn't locked again today."""
    __tablename__ = "login_attempts"
    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=False, index=True)
    ip = Column(String, default="")
    succeeded = Column(Boolean, default=False, nullable=False)
    attempted_at = Column(DateTime, default=datetime.utcnow, index=True)


class AuditLog(Base):
    """Append-only record of security-sensitive user actions.

    user_id is nullable so we can record actions that happen *to* an account
    (e.g. password reset link consumed) even when the request itself isn't
    authenticated. ``details`` is a free-form JSON string.
    """
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String, nullable=False, index=True)
    details = Column(Text, default="")
    ip_address = Column(String, default="")
    user_agent = Column(String, default="")
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)


def init_security_tables() -> None:
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Audit log helpers
# ---------------------------------------------------------------------------


# Actions worth logging. Anything not listed is rejected so typos don't
# silently disappear into the table.
AUDIT_ACTIONS = {
    "login",
    "login_failed",
    "logout",
    "signup",
    "password_change",
    "password_reset_requested",
    "password_reset_completed",
    "email_change",
    "email_verified",
    "data_export",
    "account_delete",
    "subscription_change",
    "admin_login",
}


def write_audit(
    db: Session,
    action: str,
    *,
    user_id: Optional[int] = None,
    request: Optional[Request] = None,
    details: Optional[dict] = None,
) -> None:
    """Append an audit-log entry. Best-effort: failures are swallowed so
    audit-logging never breaks the request flow."""
    if action not in AUDIT_ACTIONS:
        return
    try:
        import json as _json
        ip = ""
        ua = ""
        if request is not None:
            ip = _client_ip(request)
            ua = (request.headers.get("user-agent") or "")[:500]
        db.add(AuditLog(
            user_id=user_id,
            action=action,
            details=_json.dumps(details or {}, default=str)[:2000],
            ip_address=ip,
            user_agent=ua,
        ))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# UserSecurity accessors
# ---------------------------------------------------------------------------


def get_user_security(db: Session, user: User) -> UserSecurity:
    """Return the UserSecurity row for `user`, creating it on first access."""
    sec = db.query(UserSecurity).filter(UserSecurity.user_id == user.id).first()
    if sec:
        return sec
    sec = UserSecurity(user_id=user.id, email_verified=False)
    db.add(sec)
    db.commit()
    db.refresh(sec)
    return sec


def mark_email_verified(db: Session, user: User) -> UserSecurity:
    sec = get_user_security(db, user)
    if not sec.email_verified:
        sec.email_verified = True
        sec.email_verified_at = datetime.utcnow()
        db.commit()
    return sec


def is_email_verified(db: Session, user: User) -> bool:
    sec = get_user_security(db, user)
    return bool(sec.email_verified)


def set_admin(db: Session, user: User, admin: bool) -> UserSecurity:
    sec = get_user_security(db, user)
    sec.is_admin = bool(admin)
    db.commit()
    return sec


# ---------------------------------------------------------------------------
# Email verification tokens (stateless, signed, 24h TTL)
# ---------------------------------------------------------------------------


EMAIL_VERIFY_TTL_SECONDS = 60 * 60 * 24  # 24 hours
_EMAIL_VERIFY_SALT = "cardtax-email-verification-v1"


def _serializer() -> URLSafeTimedSerializer:
    # Reuse the session secret. Keeps the deploy story simple — one secret to
    # rotate. The salt namespaces this from any other signed payloads.
    from .auth import SESSION_SECRET
    return URLSafeTimedSerializer(SESSION_SECRET, salt=_EMAIL_VERIFY_SALT)


def generate_email_verification_token(user: User) -> str:
    return _serializer().dumps({"uid": user.id, "email": user.email or ""})


def verify_email_verification_token(token: str) -> Optional[int]:
    """Return the user_id if the token is valid and unexpired, else None."""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=EMAIL_VERIFY_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    try:
        return int(data.get("uid"))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Account lockout
# ---------------------------------------------------------------------------


LOCKOUT_THRESHOLD = 5            # failures within the window
LOCKOUT_WINDOW = timedelta(minutes=15)
LOCKOUT_DURATION = timedelta(minutes=30)


def _client_ip(request: Optional[Request]) -> str:
    if request is None:
        return ""
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def check_account_locked(db: Session, email: str) -> Optional[datetime]:
    """If `email` is currently locked, return the unlock time; else None."""
    email = (email or "").strip().lower()
    if not email:
        return None
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return None
    sec = get_user_security(db, user)
    if sec.lockout_until and sec.lockout_until > datetime.utcnow():
        return sec.lockout_until
    return None


def record_failed_login(db: Session, email: str, request: Optional[Request] = None) -> Optional[datetime]:
    """Log a failed attempt; lock the account if threshold reached. Returns the
    new lockout_until time when a lock just triggered, otherwise None."""
    email = (email or "").strip().lower()
    if not email:
        return None
    db.add(LoginAttempt(email=email, ip=_client_ip(request), succeeded=False))
    db.commit()

    user = db.query(User).filter(User.email == email).first()
    if not user:
        # We track attempts even on unknown emails for forensic purposes,
        # but we obviously can't lock a non-existent account.
        return None

    sec = get_user_security(db, user)
    cutoff = datetime.utcnow() - LOCKOUT_WINDOW
    recent_failures = (
        db.query(LoginAttempt)
        .filter(
            LoginAttempt.email == email,
            LoginAttempt.attempted_at >= cutoff,
            LoginAttempt.succeeded.is_(False),
        )
        .count()
    )
    sec.failed_login_count = recent_failures
    sec.last_failed_at = datetime.utcnow()
    if recent_failures >= LOCKOUT_THRESHOLD:
        sec.lockout_until = datetime.utcnow() + LOCKOUT_DURATION
        db.commit()
        return sec.lockout_until
    db.commit()
    return None


def record_successful_login(db: Session, user: User, request: Optional[Request] = None) -> None:
    """Reset the failure counter on a clean login."""
    db.add(LoginAttempt(
        email=user.email or "", ip=_client_ip(request), succeeded=True,
    ))
    sec = get_user_security(db, user)
    sec.failed_login_count = 0
    sec.lockout_until = None
    db.commit()


# ---------------------------------------------------------------------------
# Password strength
# ---------------------------------------------------------------------------


PASSWORD_MIN_LENGTH = 8


def validate_password_strength(password: str) -> None:
    """Raise HTTPException(400) if `password` fails the strength policy.

    Policy: 8+ chars, at least one uppercase, one lowercase, one digit. Tighter
    rules (symbols, dictionary checks) belong to a real password-strength
    library — this matches the spec.
    """
    if not isinstance(password, str):
        raise HTTPException(400, "Password is required.")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise HTTPException(400, f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if not re.search(r"[A-Z]", password):
        raise HTTPException(400, "Password must include at least one uppercase letter.")
    if not re.search(r"[a-z]", password):
        raise HTTPException(400, "Password must include at least one lowercase letter.")
    if not re.search(r"\d", password):
        raise HTTPException(400, "Password must include at least one number.")


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


CSRF_COOKIE_NAME = "cardtax_csrf"
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_SESSION_KEY = "csrf_token"

# Methods that mutate server state — these need a CSRF token.
_UNSAFE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

# Paths exempted from CSRF — third-party callbacks and signed webhooks.
_CSRF_EXEMPT_PREFIXES = (
    "/api/billing/webhook",        # signed by Stripe
    "/auth/google/callback",       # OAuth state + code
    "/api/integrations/ebay/callback",
)


def _ensure_csrf_token(request: Request) -> str:
    """Read existing CSRF token from session, or mint a new one."""
    try:
        tok = request.session.get(CSRF_SESSION_KEY)
    except (AttributeError, AssertionError):
        return ""
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = tok
    return tok


def _is_csrf_exempt(path: str) -> bool:
    return any(path.startswith(p) for p in _CSRF_EXEMPT_PREFIXES)


async def _read_form_csrf(request: Request) -> Optional[str]:
    """Pull a CSRF token out of a form body without consuming it for downstream
    handlers. We only fall back to this for non-JSON requests where the JS
    helper couldn't attach a header (e.g. native `<form>` POSTs)."""
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" not in content_type and "application/x-www-form-urlencoded" not in content_type:
        return None
    try:
        form = await request.form()
    except Exception:
        return None
    val = form.get(CSRF_FORM_FIELD)
    return str(val) if val else None


class CSRFMiddleware(BaseHTTPMiddleware):
    """Per-session CSRF protection.

    Behavior:
      * GET/HEAD/OPTIONS: ensure a token exists in the session and expose it
        in a non-HttpOnly cookie so JS can read it.
      * POST/PUT/DELETE/PATCH:
          - exempt: webhooks + OAuth callbacks
          - exempt when Content-Type is application/json — these requests are
            already same-origin-locked by CORS preflight (browsers cannot send
            JSON content-type cross-origin from a `<form>` without preflight),
            and the rest of the app uses session cookies which the browser
            attaches automatically.
          - otherwise: require X-CSRF-Token header (preferred) or csrf_token
            form field that matches the session token. 403 on mismatch.
    """

    def __init__(self, app: ASGIApp, *, cookie_secure: bool = False):
        super().__init__(app)
        self.cookie_secure = cookie_secure

    async def dispatch(self, request: Request, call_next):
        method = request.method.upper()
        path = request.url.path

        session_token = _ensure_csrf_token(request)

        if method in _UNSAFE_METHODS and not _is_csrf_exempt(path):
            content_type = (request.headers.get("content-type") or "").lower()
            is_json = content_type.startswith("application/json")
            if not is_json:
                supplied = request.headers.get(CSRF_HEADER_NAME)
                if not supplied:
                    supplied = await _read_form_csrf(request)
                if not supplied or not session_token or not hmac.compare_digest(supplied, session_token):
                    return JSONResponse(
                        {"detail": "CSRF token missing or invalid."},
                        status_code=403,
                    )

        response: Response = await call_next(request)

        # Refresh the cookie so JS always sees the current token. Non-HttpOnly
        # so the helper can read it via document.cookie.
        if session_token:
            response.set_cookie(
                CSRF_COOKIE_NAME,
                session_token,
                max_age=60 * 60 * 24 * 30,
                httponly=False,
                samesite="lax",
                secure=self.cookie_secure,
                path="/",
            )
        return response


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


# CSP per spec: self + cdnjs.cloudflare.com for scripts, fonts.googleapis.com for
# fonts. We allow inline scripts because the templates ship most behavior inline,
# and inline styles for the same reason. Tightening these would require a much
# bigger frontend refactor than fits this session.
_CSP_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://www.googletagmanager.com https://www.google-analytics.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com",
    "font-src 'self' https://fonts.gstatic.com https://fonts.googleapis.com data:",
    "img-src 'self' data: blob: https:",
    "connect-src 'self' https://www.google-analytics.com https://api.stripe.com",
    "frame-src https://js.stripe.com https://hooks.stripe.com",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach hardening headers to every response."""

    def __init__(self, app: ASGIApp, *, hsts: bool = False):
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        headers = response.headers
        headers.setdefault("Content-Security-Policy", _CSP_POLICY)
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("Permissions-Policy", "camera=(self), microphone=()")
        if self.hsts:
            headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response


# ---------------------------------------------------------------------------
# Admin auth (is_admin flag, with legacy CARDTAX_ADMIN_TOKEN fallback)
# ---------------------------------------------------------------------------


ADMIN_TOKEN_ENV = "CARDTAX_ADMIN_TOKEN"


def admin_legacy_token() -> str:
    return os.environ.get(ADMIN_TOKEN_ENV, "")


def request_admin_token(request: Request) -> str:
    return (
        request.headers.get("x-admin-token")
        or request.query_params.get("token")
        or ""
    )


def require_admin(request: Request, db: Session) -> None:
    """Allow the request if either:
      1. The signed-in user has UserSecurity.is_admin = True, OR
      2. The legacy CARDTAX_ADMIN_TOKEN env var is set and the request supplied it.

    Raises HTTPException(401) otherwise. If neither path is configured (no
    admin user exists *and* no token is set), we deny — better to lock the
    door than silently allow anyone.
    """
    # Path 1: session-based admin
    from .auth import session_user_id  # local import to avoid cycle
    uid = session_user_id(request)
    if uid:
        user = db.query(User).filter(User.id == uid).first()
        if user:
            sec = get_user_security(db, user)
            if sec.is_admin:
                return

    # Path 2: legacy token (API/CSV access for ops scripts)
    token = admin_legacy_token()
    supplied = request_admin_token(request)
    if token and supplied and hmac.compare_digest(token, supplied):
        return

    raise HTTPException(401, "admin access required")


def is_admin_user(db: Session, user: User) -> bool:
    sec = get_user_security(db, user)
    return bool(sec.is_admin)
