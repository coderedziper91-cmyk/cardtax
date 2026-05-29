"""Authentication: password hashing, sessions, Google OAuth, current_user dependency.

Sessions are signed cookies (Starlette SessionMiddleware) storing only `user_id`.
Google OAuth uses placeholder env vars; if unset, the Google button is shown
as disabled / informational.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

import bcrypt
import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from . import email_service
from .database import PasswordResetToken, TaxSettings, User, ensure_settings, get_db


# ---------------------------------------------------------------------------
# Config (env-driven; safe defaults for local dev)
# ---------------------------------------------------------------------------

SESSION_SECRET = os.environ.get(
    "CARDTAX_SESSION_SECRET",
    # Dev-only fallback. Stable across restarts so dev sessions persist.
    "cardtax-dev-insecure-secret-change-me-in-production-32+chars",
)
SESSION_COOKIE_NAME = "cardtax_session"

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback"
)
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def google_oauth_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def login_user(request: Request, user: User) -> None:
    request.session["user_id"] = user.id


def logout_user(request: Request) -> None:
    request.session.clear()


def session_user_id(request: Request) -> Optional[int]:
    try:
        uid = request.session.get("user_id")
        return int(uid) if uid is not None else None
    except (AttributeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Require an authenticated user (for API endpoints). 401 otherwise."""
    uid = session_user_id(request)
    if uid is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    user = db.query(User).filter(User.id == uid).first()
    if not user:
        request.session.clear()
        raise HTTPException(status_code=401, detail="not authenticated")
    return ensure_settings(db, user)


def maybe_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """Return user if logged in, None otherwise. Used for public pages."""
    uid = session_user_id(request)
    if uid is None:
        return None
    return db.query(User).filter(User.id == uid).first()


def require_auth_page(request: Request, db: Session = Depends(get_db)):
    """For HTML page routes: redirect to /login if not authenticated."""
    uid = session_user_id(request)
    if uid is None:
        return RedirectResponse(url="/login", status_code=303)
    user = db.query(User).filter(User.id == uid).first()
    if not user:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    ensure_settings(db, user)
    return user


# ---------------------------------------------------------------------------
# User creation
# ---------------------------------------------------------------------------


def create_user(
    db: Session,
    email: str,
    password: Optional[str] = None,
    display_name: Optional[str] = None,
    google_sub: Optional[str] = None,
    email_verified: bool = False,
) -> User:
    email = (email or "").strip().lower()
    if not email:
        raise HTTPException(400, "Email required")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(400, "An account with that email already exists")

    user = User(
        email=email,
        display_name=display_name or email.split("@")[0],
        password_hash=hash_password(password) if password else None,
        google_sub=google_sub,
        subscription_tier="free",
        subscription_status="active",
    )
    db.add(user)
    db.flush()
    db.add(TaxSettings(user_id=user.id))
    db.commit()
    db.refresh(user)

    # Initialize the security row. Google-OAuth signups are pre-verified
    # (Google already verified the address); password signups start unverified.
    from . import security as security_mod
    sec = security_mod.get_user_security(db, user)
    if email_verified and not sec.email_verified:
        security_mod.mark_email_verified(db, user)

    try:
        email_service.send_welcome(user.email or "", user.display_name or "")
    except Exception:
        pass

    return user


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------


def google_authorize_url(state: str) -> str:
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def google_exchange_code(code: str) -> dict:
    """Exchange auth code for tokens and fetch userinfo."""
    async with httpx.AsyncClient(timeout=10) as client:
        tok_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
        tok_resp.raise_for_status()
        tokens = tok_resp.json()
        access_token = tokens.get("access_token")
        if not access_token:
            raise HTTPException(400, "Google did not return an access token")

        info_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        info_resp.raise_for_status()
        return info_resp.json()


def new_oauth_state() -> str:
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

PASSWORD_RESET_TTL = timedelta(hours=1)
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")


def create_password_reset_token(db: Session, user: User) -> PasswordResetToken:
    token = PasswordResetToken(
        user_id=user.id,
        token=secrets.token_urlsafe(32),
        expires_at=datetime.utcnow() + PASSWORD_RESET_TTL,
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return token


def consume_password_reset_token(db: Session, raw_token: str) -> Optional[User]:
    """Return the User if token is valid and mark it used. None if invalid/expired/used."""
    if not raw_token:
        return None
    rec = db.query(PasswordResetToken).filter(PasswordResetToken.token == raw_token).first()
    if not rec:
        return None
    if rec.used_at is not None:
        return None
    if rec.expires_at and rec.expires_at < datetime.utcnow():
        return None
    user = db.query(User).filter(User.id == rec.user_id).first()
    if not user:
        return None
    rec.used_at = datetime.utcnow()
    db.commit()
    return user


def password_reset_url(token: str) -> str:
    base = APP_BASE_URL.rstrip("/")
    return f"{base}/reset-password?token={token}"
