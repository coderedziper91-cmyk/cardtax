"""CardTax FastAPI application."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI, Depends, HTTPException, Request, UploadFile, File, Form,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

# Sentry must initialize before the FastAPI app so its instrumentation can
# wrap the ASGI app. Only activates when SENTRY_DSN is set in the environment.
_SENTRY_DSN = (os.environ.get("SENTRY_DSN") or "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.starlette import StarletteIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration

        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            environment=os.environ.get("SENTRY_ENVIRONMENT")
                or ("production" if os.environ.get("DATABASE_URL") else "development"),
            release=os.environ.get("SENTRY_RELEASE") or None,
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE") or "0.1"),
            send_default_pii=False,
            integrations=[StarletteIntegration(), FastApiIntegration()],
        )
    except Exception:  # pragma: no cover — Sentry should never break boot
        pass

from . import auth as auth_mod
from . import billing as billing_mod
from . import card_scanner, chat_kb, csv_parsers, email_service, logging_setup, pdf_reports, ratelimit, security as security_mod, session_store, state_taxes, static_opt, tax_engine
from .integrations import ebay as ebay_mod
from .auth import (
    SESSION_COOKIE_NAME, SESSION_SECRET, create_user, current_user,
    google_authorize_url, google_exchange_code, google_oauth_configured,
    login_user, logout_user, maybe_user, new_oauth_state, require_auth_page,
    session_user_id, verify_password,
)
from .billing import (
    TIERS, TIER_ORDER, create_billing_portal, create_checkout_session,
    enforce_transaction_limit, handle_stripe_event, has_feature,
    stripe_configured, tier_info, usage_for, verify_webhook,
)
from .database import (
    Base, ChatMessage, CostBasisItem, ImportLog, MarketplaceConnection,
    PasswordResetToken, SessionLocal, TaxSettings, Transaction, User,
    WaitlistEntry, engine, ensure_settings, get_db, init_db,
)
from .security import AuditLog
from .schemas import (
    BreakAllocationRequest, CsvMapping, QuizSubmit, SettingsIn, TransactionIn,
    TransactionOut, TransactionPatch, WaitlistIn,
)
from .state_taxes import (
    compute_local_tax, compute_state_tax, list_all_local, list_local_for_state,
    list_states, salt_cap_for, STATES,
)
from .tax_engine import (
    AcquisitionType, Classification, CostBasisComponent, FilingStatus,
    HOBBY_QUIZ_FACTORS, LIKE_KIND_EXCHANGE_NOTE, allocate_break_spot,
    amt_calculation, classify_from_quiz, form_8949_preview,
    kiddie_tax_check, loss_harvest_suggestions, marginal_rate,
    nol_carryforward, ordinary_tax, qbi_deduction, quarterly_estimated_tax,
    schedule_c_preview, schedule_d_preview, self_employment_tax,
    STANDARD_DEDUCTION_2025, summarize, tax_alerts,
)


logging_setup.configure_logging(production=bool(os.environ.get("DATABASE_URL")))

app = FastAPI(title="CardTax", version="1.2.0")

# Per-request structured log + request_id context. Registered as an HTTP
# middleware (not via add_middleware) so it runs after SessionMiddleware and
# can read request.session for user_id.
app.middleware("http")(logging_setup.request_log_middleware)


def _is_local_env() -> bool:
    """True when this process is running locally (dev). Public deploys flip
    on Secure cookies and HSTS."""
    base = (os.environ.get("APP_BASE_URL") or "").lower()
    if "localhost" in base or "127.0.0.1" in base or not base:
        return True
    return os.environ.get("CARDTAX_DEV") == "1"


_SECURE_COOKIES = not _is_local_env()

# Order matters: SessionMiddleware first (innermost), then CSRF (needs
# session), then SecurityHeaders, then GZip on the outside so it compresses
# the final response (including security headers' impact on body is none —
# headers are unaffected by compression).
app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(
    security_mod.SecurityHeadersMiddleware,
    hsts=_SECURE_COOKIES,
)
app.add_middleware(
    security_mod.CSRFMiddleware,
    cookie_secure=_SECURE_COOKIES,
)
if session_store.redis_available():
    # Server-side sessions in Redis. The cookie carries only an opaque id.
    app.add_middleware(
        session_store.RedisSessionMiddleware,
        client=session_store._redis(),
        session_cookie=SESSION_COOKIE_NAME,
        max_age=60 * 60 * 24 * 30,  # 30 days
        same_site="lax",
        https_only=_SECURE_COOKIES,
    )
else:
    # No Redis — cookie-signed sessions (Starlette default).
    app.add_middleware(
        SessionMiddleware,
        secret_key=SESSION_SECRET,
        session_cookie=SESSION_COOKIE_NAME,
        max_age=60 * 60 * 24 * 30,  # 30 days
        same_site="lax",
        https_only=_SECURE_COOKIES,
    )

FRONTEND = Path(__file__).parent.parent / "frontend"
DATA_DIR = Path(__file__).parent.parent / "data"
(DATA_DIR / "uploads").mkdir(parents=True, exist_ok=True)

# In production, minify CSS/JS at startup and cache in-memory; in dev re-read
# files per request so edits show up live. ``static_assets.serve`` adds
# Cache-Control headers so browsers can cache aggressively when ``?v=`` is set.
_IS_PRODUCTION = bool(os.environ.get("DATABASE_URL"))
static_assets = static_opt.StaticAssets(
    FRONTEND / "static", production=_IS_PRODUCTION,
)


@app.get("/static/{path:path}")
def serve_static_asset(path: str, request: Request):
    return static_assets.serve(path, version=request.query_params.get("v"))


app.mount("/uploads", StaticFiles(directory=DATA_DIR / "uploads"), name="uploads")


@app.on_event("startup")
def on_startup():
    init_db()
    security_mod.init_security_tables()
    _bootstrap_admin_user()


def _bootstrap_admin_user() -> None:
    """If CARDTAX_ADMIN_EMAIL is set and that user exists, ensure they have
    `is_admin = True`. Lets ops promote an account without writing SQL."""
    email = (os.environ.get("CARDTAX_ADMIN_EMAIL") or "").strip().lower()
    if not email:
        return
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user:
            security_mod.set_admin(db, user, True)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Health check + upload size limit + graceful shutdown
# ---------------------------------------------------------------------------

# Upload limits in bytes. CSVs are bigger than photos in practice (a year of
# eBay 1099 rows is sizable), so they get a separate ceiling.
CSV_MAX_BYTES = 10 * 1024 * 1024     # 10 MB
IMAGE_MAX_BYTES = 5 * 1024 * 1024    # 5 MB
APP_VERSION = "1.0.0"


@app.get("/api/healthz")
def healthz():
    """Liveness + DB connectivity probe for load balancers and uptime checks."""
    from sqlalchemy import text as _text
    try:
        with engine.connect() as conn:
            conn.execute(_text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        return JSONResponse(
            {"status": "unhealthy", "db": f"error: {e}", "version": APP_VERSION},
            status_code=503,
        )
    return {"status": "healthy", "db": db_status, "version": APP_VERSION}


# Global API rate limit. Anonymous: 20 req/min keyed by IP. Authenticated:
# 60 req/min keyed by session user_id. Static assets and health checks are
# exempt so loading a page (many /static/* hits) doesn't trip the limit.
ANON_RATE_LIMIT = 20
AUTH_RATE_LIMIT = 60
RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_EXEMPT_PREFIXES = (
    "/static/", "/uploads/", "/api/healthz", "/api/billing/webhook",
    "/sw.js", "/manifest.json", "/robots.txt", "/sitemap.xml",
    "/favicon",
)


def _rate_limit_identity(request: Request) -> tuple[str, int]:
    """Return ``(identity_key, request_limit)`` for the current request."""
    # session_user_id reads request.session, which is populated by
    # SessionMiddleware before this middleware runs (it's nested inside).
    try:
        uid = request.session.get("user_id") if hasattr(request, "session") else None
    except (AssertionError, AttributeError):
        uid = None
    if uid:
        return f"user:{uid}", AUTH_RATE_LIMIT
    return f"ip:{ratelimit.client_ip(request)}", ANON_RATE_LIMIT


@app.middleware("http")
async def _global_rate_limit(request: Request, call_next):
    path = request.url.path
    if any(path.startswith(p) for p in _RATE_LIMIT_EXEMPT_PREFIXES):
        return await call_next(request)
    identity, limit = _rate_limit_identity(request)
    retry_after = ratelimit.consume_global(
        identity, limit=limit, window_seconds=RATE_LIMIT_WINDOW_SECONDS,
    )
    if retry_after is not None:
        return JSONResponse(
            {"detail": f"Rate limit exceeded. Retry in {retry_after} seconds."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    return await call_next(request)


@app.middleware("http")
async def _enforce_upload_size(request: Request, call_next):
    """Reject oversized uploads before they hit the parser.

    Path-based routing: ``/api/upload/*`` is CSV, ``/api/scan`` and
    transaction image uploads use the smaller image cap. Other endpoints
    pass through (FastAPI itself doesn't enforce a global limit).
    """
    if request.method in ("POST", "PUT", "PATCH"):
        path = request.url.path
        limit: Optional[int] = None
        kind = ""
        if path.startswith("/api/upload/"):
            limit, kind = CSV_MAX_BYTES, "CSV"
        elif path.startswith("/api/scan") or (
            path.startswith("/api/transactions") and "image" in path
        ):
            limit, kind = IMAGE_MAX_BYTES, "image"

        if limit is not None:
            cl = request.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > limit:
                return JSONResponse(
                    {
                        "detail": (
                            f"{kind} upload too large: "
                            f"{int(cl) // 1024} KB exceeds the "
                            f"{limit // (1024 * 1024)} MB limit."
                        )
                    },
                    status_code=413,
                )
    return await call_next(request)


# Graceful shutdown — flip a flag on SIGTERM so background loops we add later
# can observe the signal. Uvicorn/Gunicorn already wait for in-flight HTTP
# requests to finish before exiting (graceful_timeout in gunicorn.conf.py).
import signal as _signal
import threading as _threading

_shutting_down = _threading.Event()


def _handle_sigterm(signum, frame):  # pragma: no cover — signal handler
    _shutting_down.set()


try:
    _signal.signal(_signal.SIGTERM, _handle_sigterm)
except (ValueError, OSError):
    # signal.signal() only works on the main thread; tests / workers that
    # import this module from a thread will hit this. Safe to ignore.
    pass


@app.on_event("shutdown")
def _on_shutdown():
    _shutting_down.set()


# ---------------------------------------------------------------------------
# Custom error pages (404 + 500). API routes still get JSON.
# ---------------------------------------------------------------------------


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = (request.headers.get("accept") or "").lower()
    return "application/json" in accept and "text/html" not in accept


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404 and not _wants_json(request):
        return FileResponse(
            FRONTEND / "templates" / "404.html", status_code=404,
        )
    if exc.status_code >= 500 and not _wants_json(request):
        return FileResponse(
            FRONTEND / "templates" / "500.html", status_code=exc.status_code,
        )
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=dict(exc.headers or {}))


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse({"detail": exc.errors()}, status_code=422)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Last-resort handler — log to stderr, then either JSON or HTML.
    import traceback
    traceback.print_exc()
    if _SENTRY_DSN:
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
        except Exception:
            pass
    if _wants_json(request):
        return JSONResponse({"detail": "Internal server error."}, status_code=500)
    return FileResponse(FRONTEND / "templates" / "500.html", status_code=500)


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------


def _file(name: str) -> FileResponse:
    # HTML pages: no-cache so users always see the latest deploy. The static
    # CSS/JS underneath gets long cache headers via static_assets.serve.
    return FileResponse(
        FRONTEND / "templates" / name,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


def _gated_page(request: Request, db: Session, template: str):
    """Serve template if logged in, otherwise redirect to /login."""
    guard = require_auth_page(request, db)
    if isinstance(guard, RedirectResponse):
        return guard
    return _file(template)


# ---------------------------------------------------------------------------
# Helpers — DB <-> output
# ---------------------------------------------------------------------------


def _to_out(t: Transaction) -> dict:
    return {
        "id": t.id,
        "item_title": t.item_title,
        "sale_date": t.sale_date.isoformat() if t.sale_date else None,
        "sale_price": t.sale_price,
        "platform_fees": t.platform_fees,
        "shipping_charged": t.shipping_charged,
        "shipping_cost_out": t.shipping_cost_out,
        "purchase_date": t.purchase_date.isoformat() if t.purchase_date else None,
        "purchase_price": t.purchase_price,
        "grading_fees": t.grading_fees,
        "other_basis_costs": t.other_basis_costs,
        "platform_source": t.platform_source,
        "notes": t.notes or "",
        "image_path": t.image_path or "",
        "card_category": t.card_category or "",
        "acquisition_type": t.acquisition_type or "purchase",
        "donor_basis": t.donor_basis,
        "gift_date": t.gift_date.isoformat() if t.gift_date else None,
        "fmv_at_gift": t.fmv_at_gift,
        "donor_holding_period_start": t.donor_holding_period_start.isoformat() if t.donor_holding_period_start else None,
        "date_of_death": t.date_of_death.isoformat() if t.date_of_death else None,
        "fmv_at_death": t.fmv_at_death,
        "break_spot_price": t.break_spot_price,
        "break_total_fmv": t.break_total_fmv,
        "donation_date": t.donation_date.isoformat() if t.donation_date else None,
        "fmv_at_donation": t.fmv_at_donation,
        "donee_organization": t.donee_organization or "",
        "donee_unrelated_use": bool(t.donee_unrelated_use) if t.donee_unrelated_use is not None else True,
        "net_proceeds": round(t.net_proceeds, 2),
        "total_basis": round(t.total_basis, 2),
        "gain_loss": round(t.gain_loss, 2),
        "holding_period_days": t.holding_period_days,
        "is_long_term": t.is_long_term,
    }


def _db_to_engine_txn(t: Transaction) -> tax_engine.Transaction:
    basis_components = []
    if t.grading_fees:
        basis_components.append(CostBasisComponent(kind="grading", amount=t.grading_fees))
    if t.other_basis_costs:
        basis_components.append(CostBasisComponent(kind="other", amount=t.other_basis_costs))
    for b in (t.basis_items or []):
        basis_components.append(CostBasisComponent(kind=b.kind, amount=b.amount))

    atype_str = t.acquisition_type or "purchase"
    try:
        atype = AcquisitionType(atype_str)
    except ValueError:
        atype = AcquisitionType.PURCHASE

    return tax_engine.Transaction(
        id=t.id,
        item_title=t.item_title,
        sale_date=t.sale_date,
        sale_price=t.sale_price,
        platform_fees=t.platform_fees,
        shipping_charged=t.shipping_charged,
        shipping_cost_out=t.shipping_cost_out,
        purchase_date=t.purchase_date,
        purchase_price=t.purchase_price,
        basis_components=basis_components,
        platform_source=t.platform_source or "manual",
        notes=t.notes or "",
        acquisition_type=atype,
        donor_basis=t.donor_basis,
        gift_date=t.gift_date,
        fmv_at_gift=t.fmv_at_gift,
        donor_holding_period_start=t.donor_holding_period_start,
        date_of_death=t.date_of_death,
        fmv_at_death=t.fmv_at_death,
        donation_date=t.donation_date,
        fmv_at_donation=t.fmv_at_donation,
        donee_organization=t.donee_organization or "",
        donee_unrelated_use=bool(t.donee_unrelated_use) if t.donee_unrelated_use is not None else True,
        break_spot_price=t.break_spot_price,
        break_total_fmv=t.break_total_fmv,
    )


# ---------------------------------------------------------------------------
# Public page routes
# ---------------------------------------------------------------------------


def _build_landing_html() -> Optional[str]:
    """Inline critical CSS into landing.html so above-the-fold paint isn't
    blocked on /static/*.css. Built once on first request; in dev we rebuild
    every time so edits show up."""
    landing = FRONTEND / "templates" / "landing.html"
    if not landing.exists():
        return None
    html = landing.read_text(encoding="utf-8")
    try:
        app_css = static_assets.read("app.css")
        landing_css = static_assets.read("landing.css")
        critical = static_opt.extract_critical_css(app_css, landing_css)
    except Exception:
        critical = ""
    if critical:
        inject = f'<style id="critical-css">{critical}</style>\n  '
        # Insert just before the first <link rel="stylesheet">.
        html = html.replace('<link rel="stylesheet"', inject + '<link rel="stylesheet"', 1)
        # Defer the non-critical stylesheets — they keep working without JS via
        # the <noscript> fallback.
        html = html.replace(
            '<link rel="stylesheet" href="/static/app.css">',
            '<link rel="preload" as="style" href="/static/app.css" '
            'onload="this.onload=null;this.rel=\'stylesheet\'">'
            '<noscript><link rel="stylesheet" href="/static/app.css"></noscript>',
            1,
        )
        html = html.replace(
            '<link rel="stylesheet" href="/static/landing.css">',
            '<link rel="preload" as="style" href="/static/landing.css" '
            'onload="this.onload=null;this.rel=\'stylesheet\'">'
            '<noscript><link rel="stylesheet" href="/static/landing.css"></noscript>',
            1,
        )
    return html


_landing_cache: dict[str, str] = {}


def _get_landing_html() -> Optional[str]:
    if _IS_PRODUCTION:
        cached = _landing_cache.get("html")
        if cached:
            return cached
        html = _build_landing_html()
        if html:
            _landing_cache["html"] = html
        return html
    return _build_landing_html()


@app.get("/")
def root(request: Request, db: Session = Depends(get_db)):
    # Logged-in users land on the dashboard; anonymous visitors see the landing page.
    if session_user_id(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    html = _get_landing_html()
    if html is None:
        return RedirectResponse(url="/login", status_code=303)
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/login")
def page_login(request: Request):
    if session_user_id(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return _file("login.html")


@app.get("/signup")
def page_signup(request: Request):
    if session_user_id(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return _file("signup.html")


@app.get("/forgot-password")
def page_forgot_password(request: Request):
    return _file("forgot_password.html")


@app.get("/reset-password")
def page_reset_password(request: Request):
    return _file("reset_password.html")


@app.get("/onboarding")
def page_onboarding(request: Request, db: Session = Depends(get_db)):
    return _gated_page(request, db, "onboarding.html")


@app.get("/pricing")
def page_pricing():
    return _file("pricing.html")


@app.get("/terms")
def page_terms():
    return _file("terms.html")


@app.get("/privacy")
def page_privacy():
    return _file("privacy.html")


@app.get("/refund-policy")
def page_refund_policy():
    return _file("refund_policy.html")


@app.get("/blog")
def page_blog():
    return _file("blog.html")


@app.get("/blog/{slug}")
def page_blog_post(slug: str):
    safe = re.sub(r"[^a-z0-9-]", "", slug.lower())
    path = FRONTEND / "templates" / "blog" / f"{safe}.html"
    if not path.exists():
        raise HTTPException(404, "Article not found")
    return FileResponse(path)


@app.get("/help")
def page_help():
    return _file("help.html")


@app.get("/api/help/entries")
def api_help_entries():
    """Public KB content used by the /help page."""
    return {"entries": chat_kb.list_entries_full()}


@app.get("/changelog")
def page_changelog():
    return _file("changelog.html")


@app.get("/manifest.json")
def manifest_json():
    """PWA manifest — installable web app metadata."""
    return JSONResponse({
        "name": "CardTax",
        "short_name": "CardTax",
        "description": "Tax tracking for trading card sellers — Schedule C, Schedule D, Form 8949.",
        "start_url": "/dashboard",
        "scope": "/",
        "display": "standalone",
        "background_color": "#faf8f3",
        "theme_color": "#1e3a5f",
        "orientation": "portrait-primary",
        "icons": [
            {"src": "/static/favicon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
            {"src": "/static/apple-touch-icon.png", "sizes": "180x180", "type": "image/png", "purpose": "any maskable"},
        ],
        "categories": ["finance", "productivity", "business"],
    })


@app.get("/sw.js")
def service_worker():
    """Service worker for offline app-shell caching."""
    return FileResponse(
        FRONTEND / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------------------
# Authenticated page routes
# ---------------------------------------------------------------------------


@app.get("/dashboard")
def page_dashboard(request: Request, db: Session = Depends(get_db)):
    return _gated_page(request, db, "index.html")


@app.get("/transactions")
def page_transactions(request: Request, db: Session = Depends(get_db)):
    return _gated_page(request, db, "transactions.html")


@app.get("/tax-summary")
def page_tax_summary(request: Request, db: Session = Depends(get_db)):
    return _gated_page(request, db, "tax_summary.html")


@app.get("/quiz")
def page_quiz(request: Request, db: Session = Depends(get_db)):
    return _gated_page(request, db, "quiz.html")


@app.get("/upload")
def page_upload(request: Request, db: Session = Depends(get_db)):
    return _gated_page(request, db, "upload.html")


@app.get("/settings")
def page_settings(request: Request, db: Session = Depends(get_db)):
    return _gated_page(request, db, "settings.html")


@app.get("/tools")
def page_tools(request: Request, db: Session = Depends(get_db)):
    return _gated_page(request, db, "tools.html")


@app.get("/scan")
def page_scan(request: Request, db: Session = Depends(get_db)):
    return _gated_page(request, db, "scan.html")


@app.get("/account")
def page_account(request: Request, db: Session = Depends(get_db)):
    return _gated_page(request, db, "account.html")


@app.get("/connections")
def page_connections(request: Request, db: Session = Depends(get_db)):
    return _gated_page(request, db, "connections.html")


def _admin_page_guard(request: Request, db: Session):
    """Redirect to /admin/login unless either:
      * the signed-in user has is_admin = True, OR
      * the legacy CARDTAX_ADMIN_TOKEN is configured (token entry is done
        client-side; we don't redirect in that case so ops can keep using it).
    """
    uid = session_user_id(request)
    if uid:
        user = db.query(User).filter(User.id == uid).first()
        if user and security_mod.is_admin_user(db, user):
            return None
    if security_mod.admin_legacy_token():
        # Allow page render; token entry happens in the page UI.
        return None
    return RedirectResponse(url="/admin/login", status_code=303)


@app.get("/admin")
def page_admin(request: Request, db: Session = Depends(get_db)):
    redirect = _admin_page_guard(request, db)
    if redirect:
        return redirect
    admin_file = FRONTEND / "templates" / "admin.html"
    if admin_file.exists():
        return FileResponse(admin_file)
    raise HTTPException(404, "admin page not built")


@app.get("/admin/messages")
def page_admin_messages(request: Request, db: Session = Depends(get_db)):
    redirect = _admin_page_guard(request, db)
    if redirect:
        return redirect
    f = FRONTEND / "templates" / "admin_messages.html"
    if f.exists():
        return FileResponse(f)
    raise HTTPException(404, "admin messages page not built")


# ---------------------------------------------------------------------------
# Auth API — email/password
# ---------------------------------------------------------------------------


class SignupBody(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = None


class LoginBody(BaseModel):
    email: str
    password: str


class PasswordChangeBody(BaseModel):
    current_password: Optional[str] = None
    new_password: str


class EmailChangeBody(BaseModel):
    email: str


def _user_public(user: User, db: Optional[Session] = None) -> dict:
    payload = {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name or "",
        "subscription_tier": user.subscription_tier or "free",
        "subscription_status": user.subscription_status or "active",
        "has_password": bool(user.password_hash),
        "has_google": bool(user.google_sub),
        "onboarded": bool(user.onboarded_at),
    }
    if db is not None:
        sec = security_mod.get_user_security(db, user)
        payload["email_verified"] = bool(sec.email_verified)
        payload["is_admin"] = bool(sec.is_admin)
    return payload


def _send_verification_email(user: User, request: Request) -> Optional[str]:
    """Mint a token, send the email, return the URL (handy for dev when
    SendGrid isn't configured)."""
    if not user.email:
        return None
    token = security_mod.generate_email_verification_token(user)
    verify_url = f"{_base_url(request)}/verify-email?token={token}"
    try:
        email_service.send_email_verification(user.email, verify_url)
    except Exception:
        pass
    return verify_url


@app.post("/api/auth/signup")
def api_signup(body: SignupBody, request: Request, db: Session = Depends(get_db)):
    # Brake on signup-spam from a single IP.
    ratelimit.check(
        request, "auth.signup", limit=5, window_seconds=3600,
        message="Too many signups from this network. Please try again later.",
    )
    email = (body.email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Please enter a valid email address.")
    security_mod.validate_password_strength(body.password or "")
    user = create_user(
        db, email=email, password=body.password, display_name=body.display_name
    )
    dev_link = _send_verification_email(user, request)
    login_user(request, user)
    security_mod.write_audit(db, "signup", user_id=user.id, request=request,
                             details={"email": email})
    payload = _user_public(user, db)
    if dev_link and not email_service.is_configured():
        payload["dev_verification_link"] = dev_link
    return payload


@app.post("/api/auth/login")
def api_login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    # 5 attempts per IP per minute — slows credential stuffing without
    # blocking a user fumbling their password a few times.
    ratelimit.check(
        request, "auth.login", limit=5, window_seconds=60,
        message="Too many sign-in attempts. Please wait a minute and try again.",
    )
    email = (body.email or "").strip().lower()

    locked_until = security_mod.check_account_locked(db, email)
    if locked_until:
        remaining = max(1, int((locked_until - datetime.utcnow()).total_seconds() // 60))
        raise HTTPException(
            423,
            f"Account temporarily locked due to repeated failed sign-ins. "
            f"Try again in about {remaining} minute{'s' if remaining != 1 else ''}.",
        )

    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(body.password or "", user.password_hash):
        new_lock = security_mod.record_failed_login(db, email, request)
        security_mod.write_audit(
            db, "login_failed",
            user_id=user.id if user else None,
            request=request,
            details={"email": email, "locked": bool(new_lock)},
        )
        if new_lock:
            raise HTTPException(
                423,
                "Account temporarily locked due to repeated failed sign-ins. "
                "Try again in 30 minutes.",
            )
        raise HTTPException(401, "Invalid email or password.")
    ensure_settings(db, user)
    security_mod.record_successful_login(db, user, request)
    login_user(request, user)
    security_mod.write_audit(db, "login", user_id=user.id, request=request,
                             details={"method": "password"})
    return _user_public(user, db)


@app.post("/api/auth/logout")
def api_logout(request: Request, db: Session = Depends(get_db)):
    uid = session_user_id(request)
    if uid:
        security_mod.write_audit(db, "logout", user_id=uid, request=request)
    logout_user(request)
    return {"ok": True}


class DeleteAccountBody(BaseModel):
    password: Optional[str] = None


@app.post("/api/auth/delete-account")
def api_delete_account(
    body: DeleteAccountBody,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """GDPR-friendly: permanently wipe every record we hold for this user.

    Password confirmation is required for users with a local password. OAuth-only
    users are confirmed by being logged-in.
    """
    if user.password_hash:
        if not verify_password(body.password or "", user.password_hash):
            raise HTTPException(400, "Password is incorrect.")

    user_id = user.id

    # Delete data the user owns. Order matters where rows have FK relationships.
    db.query(ChatMessage).filter(ChatMessage.user_id == user_id).delete(synchronize_session=False)
    db.query(ImportLog).filter(ImportLog.user_id == user_id).delete(synchronize_session=False)
    db.query(MarketplaceConnection).filter(MarketplaceConnection.user_id == user_id).delete(synchronize_session=False)
    db.query(CostBasisItem).filter(
        CostBasisItem.transaction_id.in_(
            db.query(Transaction.id).filter(Transaction.user_id == user_id)
        )
    ).delete(synchronize_session=False)
    db.query(Transaction).filter(Transaction.user_id == user_id).delete(synchronize_session=False)
    db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user_id).delete(synchronize_session=False)
    if user.settings:
        db.delete(user.settings)
    deleted_email = user.email
    db.delete(user)
    db.commit()

    # Audit log is intentionally written *after* deletion. user_id stays in the
    # row but no longer FK-links anywhere — the FK is nullable on AuditLog.
    security_mod.write_audit(
        db, "account_delete", user_id=None, request=request,
        details={"deleted_user_id": user_id, "email": deleted_email},
    )

    logout_user(request)
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _user_public(user, db)


@app.put("/api/auth/email")
def api_change_email(
    body: EmailChangeBody,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    new_email = (body.email or "").strip().lower()
    if "@" not in new_email:
        raise HTTPException(400, "Please enter a valid email address.")
    if new_email == user.email:
        return _user_public(user, db)
    existing = db.query(User).filter(User.email == new_email, User.id != user.id).first()
    if existing:
        raise HTTPException(400, "That email is already in use.")
    old_email = user.email
    user.email = new_email
    # Changing email un-verifies the account — they need to confirm the new
    # address.
    sec = security_mod.get_user_security(db, user)
    sec.email_verified = False
    sec.email_verified_at = None
    db.commit()
    security_mod.write_audit(
        db, "email_change", user_id=user.id,
        details={"old": old_email, "new": new_email},
    )
    return _user_public(user, db)


@app.put("/api/auth/password")
def api_change_password(
    body: PasswordChangeBody,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    security_mod.validate_password_strength(body.new_password or "")
    if user.password_hash:
        if not verify_password(body.current_password or "", user.password_hash):
            raise HTTPException(400, "Current password is incorrect.")
    user.password_hash = auth_mod.hash_password(body.new_password)
    db.commit()
    security_mod.write_audit(db, "password_change", user_id=user.id, request=request)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Password reset — token-based, single-use, 1-hour TTL
# ---------------------------------------------------------------------------


PASSWORD_RESET_TTL = timedelta(hours=1)


class ForgotPasswordBody(BaseModel):
    email: str


class ResetPasswordBody(BaseModel):
    token: str
    new_password: str


def _base_url(request: Request) -> str:
    # Honor proxy headers when present so reset links work behind a load balancer.
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


@app.post("/api/auth/forgot")
def api_forgot_password(
    body: ForgotPasswordBody,
    request: Request,
    db: Session = Depends(get_db),
):
    # 3 reset requests per IP per hour — generous enough for a forgetful user
    # bouncing between devices, tight enough to keep mailers off our back.
    ratelimit.check(
        request, "auth.forgot", limit=3, window_seconds=3600,
        message="Too many reset requests. Please wait an hour and try again.",
    )
    email = (body.email or "").strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Please enter a valid email address.")

    user = db.query(User).filter(User.email == email).first()
    # Always succeed (don't leak account existence) — but only mint a token if
    # the account exists AND has a password (OAuth-only accounts can't reset).
    reset_link = None
    if user and user.password_hash:
        # Invalidate any prior unused tokens so only one is live at a time.
        (
            db.query(PasswordResetToken)
            .filter(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.used_at.is_(None),
            )
            .update({"used_at": datetime.utcnow()}, synchronize_session=False)
        )
        token = secrets.token_urlsafe(32)
        db.add(PasswordResetToken(
            user_id=user.id,
            token=token,
            expires_at=datetime.utcnow() + PASSWORD_RESET_TTL,
        ))
        db.commit()
        reset_link = f"{_base_url(request)}/reset-password?token={token}"
        try:
            email_service.send_password_reset(user.email or "", reset_link)
        except Exception:
            pass
        security_mod.write_audit(
            db, "password_reset_requested", user_id=user.id, request=request,
        )

    return {
        "ok": True,
        "message": (
            "If an account exists for that email, a reset link has been generated."
        ),
        # Dev convenience: only surface the link in-browser when SendGrid isn't
        # configured (i.e. the email went to the server console).
        "dev_reset_link": reset_link if not email_service.is_configured() else None,
    }


@app.post("/api/auth/reset")
def api_reset_password(
    body: ResetPasswordBody,
    request: Request,
    db: Session = Depends(get_db),
):
    # Stop a bad actor from grinding the token space.
    ratelimit.check(
        request, "auth.reset", limit=10, window_seconds=600,
        message="Too many attempts. Please wait a few minutes.",
    )
    security_mod.validate_password_strength(body.new_password or "")
    tok = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token == (body.token or ""))
        .first()
    )
    if not tok:
        raise HTTPException(400, "This reset link is invalid.")
    if tok.used_at is not None:
        raise HTTPException(400, "This reset link has already been used.")
    if tok.expires_at < datetime.utcnow():
        raise HTTPException(400, "This reset link has expired. Request a new one.")
    user = db.query(User).filter(User.id == tok.user_id).first()
    if not user:
        raise HTTPException(400, "Account no longer exists.")

    user.password_hash = auth_mod.hash_password(body.new_password)
    tok.used_at = datetime.utcnow()
    db.commit()
    security_mod.write_audit(db, "password_reset_completed", user_id=user.id, request=request)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


@app.get("/verify-email")
def page_verify_email(token: str = "", db: Session = Depends(get_db)):
    """User clicks the link in their email — verify the token and bounce them
    to the dashboard (or login if they're signed out)."""
    uid = security_mod.verify_email_verification_token(token)
    if uid is None:
        return RedirectResponse(url="/account?verify=expired", status_code=303)
    user = db.query(User).filter(User.id == uid).first()
    if not user:
        return RedirectResponse(url="/login?verify=invalid", status_code=303)
    security_mod.mark_email_verified(db, user)
    return RedirectResponse(url="/account?verify=ok", status_code=303)


@app.post("/api/auth/resend-verification")
def api_resend_verification(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    ratelimit.check(
        request, "auth.resend_verify", limit=3, window_seconds=3600,
        message="Too many verification resends. Please wait an hour.",
    )
    if security_mod.is_email_verified(db, user):
        return {"ok": True, "already_verified": True}
    dev_link = _send_verification_email(user, request)
    out = {"ok": True, "already_verified": False}
    if dev_link and not email_service.is_configured():
        out["dev_verification_link"] = dev_link
    return out


@app.get("/api/csrf-token")
def api_csrf_token(request: Request):
    """JS bootstrap fetches this on first load if it can't read the cookie
    (e.g. fresh session without any prior GET). The middleware also seeds the
    token in the session via `_ensure_csrf_token`, so this just exposes it."""
    token = request.session.get(security_mod.CSRF_SESSION_KEY) or ""
    return {"csrf_token": token}


@app.post("/api/auth/complete-onboarding")
def api_complete_onboarding(
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    if not user.onboarded_at:
        user.onboarded_at = datetime.utcnow()
        db.commit()
    return _user_public(user)


@app.get("/api/auth/reset/check")
def api_reset_check(token: str, db: Session = Depends(get_db)):
    """Lets the reset page render the right state before the user types."""
    tok = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token == (token or ""))
        .first()
    )
    if not tok:
        return {"valid": False, "reason": "invalid"}
    if tok.used_at is not None:
        return {"valid": False, "reason": "used"}
    if tok.expires_at < datetime.utcnow():
        return {"valid": False, "reason": "expired"}
    return {"valid": True}


# ---------------------------------------------------------------------------
# Email preferences
# ---------------------------------------------------------------------------


class EmailPrefsBody(BaseModel):
    tax_deadlines: Optional[bool] = None
    subscription: Optional[bool] = None
    weekly_digest: Optional[bool] = None


def _email_prefs_out(user: User) -> dict:
    return {
        "tax_deadlines": bool(getattr(user, "email_pref_tax_deadlines", True)),
        "subscription": bool(getattr(user, "email_pref_subscription", True)),
        "weekly_digest": bool(getattr(user, "email_pref_weekly_digest", True)),
        "sendgrid_configured": email_service.is_configured(),
    }


@app.get("/api/auth/email-prefs")
def api_get_email_prefs(user: User = Depends(current_user)):
    return _email_prefs_out(user)


@app.put("/api/auth/email-prefs")
def api_put_email_prefs(
    body: EmailPrefsBody,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if body.tax_deadlines is not None:
        user.email_pref_tax_deadlines = bool(body.tax_deadlines)
    if body.subscription is not None:
        user.email_pref_subscription = bool(body.subscription)
    if body.weekly_digest is not None:
        user.email_pref_weekly_digest = bool(body.weekly_digest)
    db.commit()
    return _email_prefs_out(user)


@app.post("/api/auth/email-prefs/test")
def api_email_prefs_test(user: User = Depends(current_user)):
    """Send a no-op welcome email to the current user — handy for verifying SendGrid setup."""
    if not user.email:
        raise HTTPException(400, "Your account has no email address.")
    sent = email_service.send_welcome(user.email, user.display_name or "")
    return {"sent": bool(sent), "sendgrid_configured": email_service.is_configured()}


# ---------------------------------------------------------------------------
# Data export (GDPR-friendly) — full account download as a ZIP.
# ---------------------------------------------------------------------------


def _transactions_csv(db: Session, user: User) -> str:
    rows = (
        db.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.deleted_at.is_(None))
        .order_by(Transaction.sale_date.asc(), Transaction.id.asc())
        .all()
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "id", "sale_date", "item_title", "platform_source", "card_category",
        "sale_price", "platform_fees", "shipping_charged", "shipping_cost_out",
        "purchase_date", "purchase_price", "grading_fees", "other_basis_costs",
        "acquisition_type", "donor_basis", "fmv_at_gift", "fmv_at_death",
        "donation_date", "fmv_at_donation", "donee_organization",
        "net_proceeds", "total_basis", "gain_loss",
        "holding_period_days", "is_long_term", "notes",
    ])
    for t in rows:
        w.writerow([
            t.id,
            t.sale_date.isoformat() if t.sale_date else "",
            t.item_title or "",
            t.platform_source or "manual",
            t.card_category or "",
            f"{t.sale_price:.2f}",
            f"{t.platform_fees:.2f}",
            f"{t.shipping_charged:.2f}",
            f"{t.shipping_cost_out:.2f}",
            t.purchase_date.isoformat() if t.purchase_date else "",
            f"{t.purchase_price:.2f}",
            f"{t.grading_fees:.2f}",
            f"{t.other_basis_costs:.2f}",
            t.acquisition_type or "purchase",
            "" if t.donor_basis is None else f"{t.donor_basis:.2f}",
            "" if t.fmv_at_gift is None else f"{t.fmv_at_gift:.2f}",
            "" if t.fmv_at_death is None else f"{t.fmv_at_death:.2f}",
            t.donation_date.isoformat() if t.donation_date else "",
            "" if t.fmv_at_donation is None else f"{t.fmv_at_donation:.2f}",
            t.donee_organization or "",
            f"{t.net_proceeds:.2f}",
            f"{t.total_basis:.2f}",
            f"{t.gain_loss:.2f}",
            t.holding_period_days if t.holding_period_days is not None else "",
            "true" if t.is_long_term else "false",
            (t.notes or "").replace("\n", " "),
        ])
    return buf.getvalue()


def _tax_settings_json(user: User) -> str:
    s = user.settings
    payload = {
        "tax_year": s.tax_year,
        "filing_status": s.filing_status,
        "state": s.state or "",
        "city": s.city or "",
        "ordinary_income_estimate": s.ordinary_income_estimate,
        "classification": s.classification,
        "lot_method": s.lot_method,
        "prior_year_tax": s.prior_year_tax or 0.0,
        "prior_year_agi": s.prior_year_agi or 0.0,
        "withholding_paid": s.withholding_paid or 0.0,
        "is_kiddie_filer": bool(s.is_kiddie_filer),
        "parent_marginal_rate": s.parent_marginal_rate or 0.22,
        "nol_carryforward": s.nol_carryforward or 0.0,
        "quiz_answers": json.loads(s.quiz_answers or "{}"),
        "email_preferences": {
            "tax_deadlines": bool(getattr(user, "email_pref_tax_deadlines", True)),
            "subscription": bool(getattr(user, "email_pref_subscription", True)),
            "weekly_digest": bool(getattr(user, "email_pref_weekly_digest", True)),
        },
    }
    return json.dumps(payload, indent=2, default=str)


def _import_history_csv(db: Session, user: User) -> str:
    rows = (
        db.query(ImportLog)
        .filter(ImportLog.user_id == user.id)
        .order_by(ImportLog.started_at.asc())
        .all()
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "started_at", "platform", "source", "status", "created", "skipped", "message"])
    for r in rows:
        w.writerow([
            r.id,
            r.started_at.isoformat() if r.started_at else "",
            r.platform or "",
            r.source or "",
            r.status or "",
            r.created or 0,
            r.skipped or 0,
            (r.message or "").replace("\n", " "),
        ])
    return buf.getvalue()


def _chat_history_csv(db: Session, user: User) -> str:
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .all()
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "created_at", "role", "content", "matched_kb_key", "needs_human", "answered"])
    for m in rows:
        w.writerow([
            m.id,
            m.created_at.isoformat() if m.created_at else "",
            m.role or "",
            (m.content or "").replace("\r", " ").replace("\n", "\\n"),
            m.matched_kb_key or "",
            "true" if m.needs_human else "false",
            "true" if m.answered else "false",
        ])
    return buf.getvalue()


def _account_summary_json(user: User) -> str:
    payload = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "user": {
            "id": user.id,
            "email": user.email or "",
            "display_name": user.display_name or "",
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "subscription_tier": user.subscription_tier or "free",
            "subscription_status": user.subscription_status or "active",
            "current_period_end": user.current_period_end.isoformat() if user.current_period_end else None,
            "has_password": bool(user.password_hash),
            "has_google": bool(user.google_sub),
        },
        "files": {
            "transactions.csv": "All your imported and manually-added card transactions.",
            "tax_settings.json": "Your tax-year settings, classification, and email preferences.",
            "import_history.csv": "Every CSV upload and marketplace sync attempt.",
            "chat_history.csv": "Your in-app chat with the bot/team.",
        },
    }
    return json.dumps(payload, indent=2)


@app.get("/api/account/export")
def api_account_export(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Return a ZIP containing every piece of user data we hold.

    Streamed straight from memory — files are small (a few MB at most for the
    largest tier limits).
    """
    import zipfile

    today = date.today().isoformat()
    safe_email = re.sub(r"[^a-zA-Z0-9._-]", "_", (user.email or f"user{user.id}"))[:60]
    filename = f"cardtax-export-{safe_email}-{today}.zip"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", (
            "CardTax — data export\n"
            f"Exported: {datetime.utcnow().isoformat()}Z\n"
            f"Account:  {user.email or '(no email)'}\n\n"
            "Contents:\n"
            "  account.json         — account metadata and subscription state\n"
            "  transactions.csv     — every transaction in your account\n"
            "  tax_settings.json    — tax year, filing status, classification, prefs\n"
            "  import_history.csv   — CSV and API import attempts\n"
            "  chat_history.csv     — your in-app chat history\n\n"
            "This export includes every piece of data CardTax holds about your account.\n"
        ))
        zf.writestr("account.json", _account_summary_json(user))
        zf.writestr("transactions.csv", _transactions_csv(db, user))
        zf.writestr("tax_settings.json", _tax_settings_json(user))
        zf.writestr("import_history.csv", _import_history_csv(db, user))
        zf.writestr("chat_history.csv", _chat_history_csv(db, user))

    security_mod.write_audit(db, "data_export", user_id=user.id, request=request)

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Auth API — Google OAuth
# ---------------------------------------------------------------------------


@app.get("/api/auth/google/configured")
def api_google_configured():
    return {"configured": google_oauth_configured()}


@app.get("/auth/google/start")
def auth_google_start(request: Request):
    if not google_oauth_configured():
        return RedirectResponse(
            url="/login?error=google_not_configured", status_code=303
        )
    state = new_oauth_state()
    request.session["oauth_state"] = state
    return RedirectResponse(url=google_authorize_url(state), status_code=303)


@app.get("/auth/google/callback")
async def auth_google_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if error:
        return RedirectResponse(url=f"/login?error={error}", status_code=303)
    if not code:
        return RedirectResponse(url="/login?error=missing_code", status_code=303)
    expected = request.session.pop("oauth_state", None)
    if not expected or expected != state:
        return RedirectResponse(url="/login?error=state_mismatch", status_code=303)

    try:
        userinfo = await google_exchange_code(code)
    except Exception:
        return RedirectResponse(url="/login?error=google_exchange_failed", status_code=303)

    sub = userinfo.get("sub")
    email = (userinfo.get("email") or "").strip().lower()
    name = userinfo.get("name") or userinfo.get("given_name") or ""
    if not sub or not email:
        return RedirectResponse(url="/login?error=google_missing_fields", status_code=303)

    user = db.query(User).filter(User.google_sub == sub).first()
    if not user:
        user = db.query(User).filter(User.email == email).first()
        if user:
            user.google_sub = sub
            if not user.display_name:
                user.display_name = name
            db.commit()
            # Google has already verified this address — trust it.
            security_mod.mark_email_verified(db, user)
        else:
            user = create_user(
                db, email=email, display_name=name, google_sub=sub,
                email_verified=True,
            )
    else:
        # Returning Google user — make sure they're marked verified.
        security_mod.mark_email_verified(db, user)
    ensure_settings(db, user)
    login_user(request, user)
    dest = "/dashboard" if user.onboarded_at else "/onboarding"
    return RedirectResponse(url=dest, status_code=303)


# ---------------------------------------------------------------------------
# Billing API
# ---------------------------------------------------------------------------


class CheckoutBody(BaseModel):
    tier: str
    cycle: Optional[str] = "monthly"  # "monthly" or "yearly"


@app.get("/api/billing/plans")
def api_billing_plans():
    return {
        "tiers": [
            {
                "key": k,
                "name": TIERS[k]["name"],
                "price_display": TIERS[k]["price_display"],
                "price_monthly": TIERS[k]["price_monthly"],
                "price_yearly": TIERS[k].get("price_yearly", 0.0),
                "price_yearly_display": TIERS[k].get("price_yearly_display", "$0"),
                "yearly_savings_pct": TIERS[k].get("yearly_savings_pct", 0),
                "transactions_per_year": TIERS[k]["transactions_per_year"],
                "tagline": TIERS[k]["tagline"],
                "features": TIERS[k]["features"],
                "not_included": TIERS[k].get("not_included", []),
                "cta": TIERS[k]["cta"],
                "badge": TIERS[k].get("badge"),
            }
            for k in TIER_ORDER
        ],
        "stripe_configured": stripe_configured(),
    }


@app.get("/api/billing/status")
def api_billing_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return usage_for(db, user)


@app.post("/api/billing/checkout")
def api_billing_checkout(
    body: CheckoutBody,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    url = create_checkout_session(db, user, body.tier)
    return {"url": url}


@app.post("/api/billing/portal")
def api_billing_portal(user: User = Depends(current_user), db: Session = Depends(get_db)):
    url = create_billing_portal(user, db)
    return {"url": url}


@app.post("/api/billing/webhook")
async def api_billing_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    event = verify_webhook(payload, sig)
    # Persist the webhook for retry/observability before processing, so a crash
    # mid-processing doesn't drop the event entirely. handle_stripe_event is
    # idempotent (it just mirrors Stripe state onto the user row).
    wh = billing_mod.record_webhook(db, event, payload, sig)
    try:
        handle_stripe_event(db, event)
        billing_mod.mark_webhook_processed(db, wh)
    except Exception as e:  # never break Stripe; log + 200
        billing_mod.mark_webhook_failed(db, wh, str(e))
        return JSONResponse({"received": True, "error": str(e)}, status_code=200)

    # Best-effort retry of any earlier failures whenever a new webhook lands.
    try:
        billing_mod.retry_failed_webhooks(db, handle_stripe_event, max_attempts=5)
    except Exception:
        pass

    # Audit subscription-state changes (after we've mirrored them onto the user).
    etype = event.get("type", "")
    if etype.startswith("customer.subscription.") or etype == "checkout.session.completed":
        obj = (event.get("data") or {}).get("object") or {}
        customer_id = obj.get("customer")
        if customer_id:
            user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
            if user:
                security_mod.write_audit(
                    db, "subscription_change", user_id=user.id,
                    details={"event_type": etype, "tier": user.subscription_tier,
                             "status": user.subscription_status},
                )
    return {"received": True}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@app.get("/api/settings")
def get_settings(user: User = Depends(current_user)):
    s = user.settings
    return {
        "tax_year": s.tax_year,
        "filing_status": s.filing_status,
        "state": s.state or "",
        "city": s.city or "",
        "ordinary_income_estimate": s.ordinary_income_estimate,
        "classification": s.classification,
        "lot_method": s.lot_method,
        "prior_year_tax": s.prior_year_tax or 0.0,
        "prior_year_agi": s.prior_year_agi or 0.0,
        "withholding_paid": s.withholding_paid or 0.0,
        "is_kiddie_filer": bool(s.is_kiddie_filer) if s.is_kiddie_filer is not None else False,
        "parent_marginal_rate": s.parent_marginal_rate or 0.22,
        "nol_carryforward": s.nol_carryforward or 0.0,
    }


@app.put("/api/settings")
def put_settings(
    body: SettingsIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    s = user.settings
    s.tax_year = body.tax_year
    s.filing_status = body.filing_status
    s.state = (body.state or "").upper()
    s.city = body.city or ""
    s.ordinary_income_estimate = body.ordinary_income_estimate
    s.classification = body.classification
    s.lot_method = body.lot_method
    s.prior_year_tax = body.prior_year_tax
    s.prior_year_agi = body.prior_year_agi
    s.withholding_paid = body.withholding_paid
    s.is_kiddie_filer = body.is_kiddie_filer
    s.parent_marginal_rate = body.parent_marginal_rate
    s.nol_carryforward = body.nol_carryforward
    db.commit()
    return {"ok": True}


@app.get("/api/states")
def get_states():
    return {"states": list_states()}


@app.get("/api/cities")
def get_cities(state: Optional[str] = None):
    if state:
        return {"cities": list_local_for_state(state)}
    return {"cities": list_all_local()}


# ---------------------------------------------------------------------------
# Transactions CRUD
# ---------------------------------------------------------------------------


@app.get("/api/transactions")
def list_transactions(
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    txns = (
        db.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.deleted_at.is_(None))
        .order_by(Transaction.sale_date.desc())
        .all()
    )
    return [_to_out(t) for t in txns]


@app.post("/api/transactions")
def create_transaction(
    body: TransactionIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    enforce_transaction_limit(db, user, additional=1)
    data = body.model_dump()
    t = Transaction(user_id=user.id, **data)
    db.add(t)
    db.commit()
    db.refresh(t)
    return _to_out(t)


@app.patch("/api/transactions/{txn_id}")
def patch_transaction(
    txn_id: int,
    body: TransactionPatch,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    t = db.query(Transaction).filter(
        Transaction.id == txn_id,
        Transaction.user_id == user.id,
        Transaction.deleted_at.is_(None),
    ).first()
    if not t:
        raise HTTPException(404, "not found")
    for field_name, value in body.model_dump(exclude_unset=True).items():
        setattr(t, field_name, value)
    db.commit()
    db.refresh(t)
    return _to_out(t)


@app.delete("/api/transactions/{txn_id}")
def delete_transaction(
    txn_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    # Soft delete: set deleted_at instead of removing the row. The transaction
    # disappears from list/summary/export queries but stays restorable.
    t = db.query(Transaction).filter(
        Transaction.id == txn_id,
        Transaction.user_id == user.id,
        Transaction.deleted_at.is_(None),
    ).first()
    if not t:
        raise HTTPException(404, "not found")
    t.deleted_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.post("/api/transactions/{txn_id}/restore")
def restore_transaction(
    txn_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Undo a soft-delete. 404 if the txn doesn't exist or isn't deleted."""
    t = db.query(Transaction).filter(
        Transaction.id == txn_id,
        Transaction.user_id == user.id,
        Transaction.deleted_at.isnot(None),
    ).first()
    if not t:
        raise HTTPException(404, "not found")
    t.deleted_at = None
    db.commit()
    db.refresh(t)
    return _to_out(t)


@app.delete("/api/transactions")
def delete_all_transactions(
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    now = datetime.utcnow()
    (
        db.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.deleted_at.is_(None))
        .update({Transaction.deleted_at: now}, synchronize_session=False)
    )
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# CSV upload
# ---------------------------------------------------------------------------


@app.post("/api/upload/preview")
async def upload_preview(
    file: UploadFile = File(...),
    platform_hint: Optional[str] = Form(None),
    user: User = Depends(current_user),
):
    text_data = (await file.read()).decode("utf-8", errors="replace")
    headers = csv_parsers.csv_headers(text_data)
    detected = csv_parsers.detect_platform(headers)
    platform = platform_hint or detected
    sample: list[dict] = []
    if platform in ("ebay", "whatnot", "comc", "myslabs", "tcgplayer", "mercari"):
        sales, _ = csv_parsers.parse_csv(text_data, platform_hint=platform)
        for s in sales[:10]:
            sample.append({
                "item_title": s.item_title,
                "sale_date": s.sale_date.isoformat(),
                "sale_price": s.sale_price,
                "platform_fees": s.platform_fees,
                "shipping_charged": s.shipping_charged,
                "shipping_cost_out": s.shipping_cost_out,
            })
    return {
        "detected_platform": detected,
        "platform": platform,
        "headers": headers,
        "sample": sample,
        "total_preview": len(sample),
    }


@app.post("/api/upload/import")
async def upload_import(
    file: UploadFile = File(...),
    platform: str = Form("auto"),
    mapping: str = Form("{}"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    text_data = (await file.read()).decode("utf-8", errors="replace")
    mapping_dict = json.loads(mapping or "{}") if mapping else {}
    platform_hint = None if platform == "auto" else platform
    sales, detected = csv_parsers.parse_csv(
        text_data, platform_hint=platform_hint, mapping=mapping_dict or None,
    )
    # Enforce yearly transaction limit before importing anything
    enforce_transaction_limit(db, user, additional=len(sales))

    # De-dupe by (sale_date, item_title, sale_price, platform_source) across
    # the user's existing transactions and within this batch.
    existing = {
        (t.sale_date, (t.item_title or "").strip().lower(),
         round(t.sale_price or 0.0, 2), (t.platform_source or "").lower())
        for t in db.query(Transaction).filter(Transaction.user_id == user.id).all()
    }

    created = 0
    skipped = 0
    for s in sales:
        key = (
            s.sale_date,
            (s.item_title or "").strip().lower(),
            round(s.sale_price or 0.0, 2),
            (s.platform_source or "").lower(),
        )
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        t = Transaction(
            user_id=user.id,
            item_title=s.item_title,
            sale_date=s.sale_date,
            sale_price=s.sale_price,
            platform_fees=s.platform_fees,
            shipping_charged=s.shipping_charged,
            shipping_cost_out=s.shipping_cost_out,
            platform_source=s.platform_source,
        )
        db.add(t)
        created += 1
    db.commit()
    return {"created": created, "skipped": skipped, "platform": detected}


# ---------------------------------------------------------------------------
# Tax summary, dashboard, schedule previews
# ---------------------------------------------------------------------------


def _build_summary(db: Session, user: User):
    txns = (
        db.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.deleted_at.is_(None))
        .all()
    )
    s = user.settings
    engine_txns = [_db_to_engine_txn(t) for t in txns]
    fs = FilingStatus(s.filing_status)
    cls = Classification(s.classification)
    summary = summarize(
        transactions=engine_txns,
        ordinary_income=s.ordinary_income_estimate,
        filing_status=fs,
        classification=cls,
    )

    # State tax
    state_code = (s.state or "").upper()
    if state_code:
        st = compute_state_tax(
            state_code=state_code,
            ordinary_income=s.ordinary_income_estimate,
            short_term_gain=max(0.0, summary.net_short_term),
            long_term_collectible_gain=max(0.0, summary.net_long_term),
            filing_status=s.filing_status,
        )
        summary.state_tax = st["total_state_tax"]
        summary.state_code = state_code
        summary.estimated_total_tax += summary.state_tax
        for n in st.get("notes", []):
            summary.notes.append(f"[{state_code}] {n}")
    else:
        st = None

    # Local / city tax
    local = None
    if s.city:
        local_taxable = (
            s.ordinary_income_estimate
            + max(0.0, summary.net_short_term)
            + max(0.0, summary.net_long_term)
        )
        local = compute_local_tax(
            locality_key=s.city,
            taxable_income=local_taxable,
            state_tax_total=summary.state_tax,
            se_earnings=max(0.0, summary.gross_receipts - summary.total_basis - summary.total_fees) if cls == Classification.DEALER else 0.0,
            filing_status=s.filing_status,
        )
        summary.estimated_total_tax += local["total_local_tax"]
        for n in local.get("notes", []):
            summary.notes.append(f"[{local.get('city','Local')}] {n}")

    # SALT cap (Schedule A federal deduction)
    magi = s.ordinary_income_estimate + max(0.0, summary.total_net)
    cap, salt_note = salt_cap_for(s.tax_year, s.filing_status, magi)
    summary.salt_cap = cap
    summary.salt_note = salt_note

    return summary, engine_txns, st, local


def _summary_to_dict(summary) -> dict:
    d = summary.__dict__.copy()
    d["classification"] = summary.classification.value
    d["filing_status"] = summary.filing_status.value
    return d


def _portfolio_series(engine_txns) -> dict:
    sales = sorted([t for t in engine_txns if not t.is_donation], key=lambda t: t.sale_date)
    points: list[dict] = []
    cum_proceeds = 0.0
    cum_basis = 0.0
    cum_gain = 0.0
    for t in sales:
        cum_proceeds += t.net_proceeds
        cum_basis += t.total_basis
        cum_gain += t.gain_loss
        points.append({
            "date": t.sale_date.isoformat(),
            "cum_proceeds": round(cum_proceeds, 2),
            "cum_basis": round(cum_basis, 2),
            "cum_gain": round(cum_gain, 2),
        })
    return {"points": points}


def _allocation_breakdowns(engine_txns) -> dict:
    by_platform: dict[str, float] = {}
    by_category: dict[str, float] = {}
    for t in engine_txns:
        if t.is_donation:
            continue
        by_platform[t.platform_source or "unknown"] = (
            by_platform.get(t.platform_source or "unknown", 0.0) + t.sale_price
        )
    return {
        "by_platform": [{"label": k, "value": round(v, 2)} for k, v in by_platform.items()],
        "by_category": [{"label": k, "value": round(v, 2)} for k, v in by_category.items()],
    }


def _settings_snapshot(s) -> dict:
    return {
        "tax_year": s.tax_year,
        "filing_status": s.filing_status,
        "state": s.state or "",
        "city": s.city or "",
        "classification": s.classification,
        "ordinary_income_estimate": s.ordinary_income_estimate,
    }


def _maybe_send_deadline_reminder(db: Session, user: User, per_quarter_amount: float) -> None:
    """Fire a quarterly-deadline email at most once per (year,quarter) per user.

    Triggered opportunistically off dashboard loads — keeps the wiring simple
    without needing a cron. The `last_deadline_notified` column is the
    idempotency key (e.g., "2026-Q2").
    """
    if not getattr(user, "email_pref_tax_deadlines", True):
        return
    if not user.email:
        return
    today = date.today()
    try:
        label, due = email_service.next_deadline(today)
    except Exception:
        return
    delta = (due - today).days
    if delta < 0 or delta > 14:
        return
    key = f"{due.year}-{label}"
    if (user.last_deadline_notified or "") == key:
        return
    try:
        email_service.send_tax_deadline_reminder(
            user.email, label, due.isoformat(), per_quarter_amount or None,
        )
        user.last_deadline_notified = key
        db.commit()
    except Exception:
        pass


@app.get("/api/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    summary, engine_txns, state_detail, local_detail = _build_summary(db, user)
    suggestions = loss_harvest_suggestions(engine_txns)
    quarterly = quarterly_estimated_tax(
        estimated_annual_tax=summary.estimated_total_tax,
        prior_year_tax=user.settings.prior_year_tax or 0.0,
        prior_year_agi=user.settings.prior_year_agi or 0.0,
        withholding_paid=user.settings.withholding_paid or 0.0,
        year=user.settings.tax_year,
    )
    _maybe_send_deadline_reminder(db, user, float(quarterly.get("per_quarter") or 0.0))
    return {
        "summary": _summary_to_dict(summary),
        "state_detail": state_detail,
        "local_detail": local_detail,
        "tx_count": len(engine_txns),
        "suggestions": suggestions,
        "alerts": tax_alerts(summary, quarterly),
        "portfolio_series": _portfolio_series(engine_txns),
        "allocation": _allocation_breakdowns(engine_txns),
        "settings": _settings_snapshot(user.settings),
        "usage": usage_for(db, user),
    }


@app.get("/api/tax-summary")
def api_tax_summary(user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = user.settings
    summary, engine_txns, state_detail, local_detail = _build_summary(db, user)
    cls = Classification(s.classification)
    fs = FilingStatus(s.filing_status)
    payload = {
        "summary": _summary_to_dict(summary),
        "state_detail": state_detail,
        "local_detail": local_detail,
        "settings": _settings_snapshot(s),
    }

    sale_txns = [t for t in engine_txns if not t.is_donation]

    if cls == Classification.DEALER:
        payload["schedule_c"] = schedule_c_preview(sale_txns)

        net_se = (
            summary.gross_receipts - summary.total_basis - summary.total_fees
        )
        payload["self_employment_tax"] = self_employment_tax(net_se, fs)

        std_ded = STANDARD_DEDUCTION_2025[fs.value]
        ti_before_qbi = max(0.0, s.ordinary_income_estimate + max(0.0, net_se) - std_ded)
        payload["qbi"] = qbi_deduction(
            qualified_business_income=max(0.0, net_se),
            taxable_income_before_qbi=ti_before_qbi,
            net_capital_gain=0.0,
            filing_status=fs,
            w2_wages_paid=0.0,
            qualified_property_basis=0.0,
            is_sstb=False,
        )

        payload["nol"] = nol_carryforward(
            current_year_business_loss=min(0.0, net_se),
            prior_year_nol_carryforward=s.nol_carryforward or 0.0,
            current_year_taxable_income=ti_before_qbi,
        )
    else:
        payload["schedule_d"] = schedule_d_preview(engine_txns)

    # Form 8949 — gated by plan; non-Unlimited users see a teaser only
    if has_feature(user, "form_8949_export"):
        payload["form_8949"] = form_8949_preview(engine_txns)
    else:
        payload["form_8949"] = {
            "locked": True,
            "reason": "Form 8949 detailed export is included with the Unlimited plan.",
        }

    payload["suggestions"] = loss_harvest_suggestions(engine_txns)

    donations = [t for t in engine_txns if t.is_donation]
    payload["donations"] = [
        {
            "item_title": d.item_title,
            "donation_date": (d.donation_date or d.sale_date).isoformat(),
            "donee_organization": d.donee_organization,
            "fmv": d.fmv_at_donation or d.sale_price or 0.0,
            "basis": d.total_basis,
            "long_term": d.is_long_term,
            "unrelated_use": d.donee_unrelated_use,
            "deduction_used": (d.fmv_at_donation or d.sale_price or 0.0)
                              if (d.is_long_term and not d.donee_unrelated_use)
                              else d.total_basis,
        }
        for d in donations
    ]

    payload["personal_use_disallowed"] = [
        {"item_title": t.item_title, "loss": round(t.personal_use_disallowed_loss, 2)}
        for t in engine_txns if t.personal_use_disallowed_loss > 0
    ]

    payload["quarterly"] = quarterly_estimated_tax(
        estimated_annual_tax=summary.estimated_total_tax,
        prior_year_tax=s.prior_year_tax or 0.0,
        prior_year_agi=s.prior_year_agi or 0.0,
        withholding_paid=s.withholding_paid or 0.0,
        year=s.tax_year,
    )

    std_ded = STANDARD_DEDUCTION_2025[fs.value]
    regular_ti = max(0.0, s.ordinary_income_estimate - std_ded)
    payload["amt"] = amt_calculation(
        regular_taxable_income=regular_ti,
        collectible_lt_gain=max(0.0, summary.net_long_term),
        filing_status=fs,
        regular_tax=summary.federal_income_tax,
    )

    if s.is_kiddie_filer:
        child_marg = marginal_rate(regular_ti + summary.total_net, fs)
        payload["kiddie_tax"] = kiddie_tax_check(
            child_unearned_income=max(0.0, summary.total_net),
            child_is_dependent_under_24=True,
            parent_marginal_rate=s.parent_marginal_rate or 0.22,
            child_marginal_rate=child_marg,
        )

    payload["alerts"] = tax_alerts(summary, payload["quarterly"])
    payload["like_kind_note"] = LIKE_KIND_EXCHANGE_NOTE
    return payload


# ---------------------------------------------------------------------------
# Tax report PDF downloads
# ---------------------------------------------------------------------------


def _pdf_response(pdf_bytes: bytes, filename: str) -> Response:
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _require_verified_email(db: Session, user: User) -> None:
    """Gate features that should not work for unverified accounts."""
    if not security_mod.is_email_verified(db, user):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "email_unverified",
                "message": "Please verify your email to use this feature. Check your inbox or resend from your account page.",
            },
        )


def _settings_dict(s) -> dict:
    return {
        "tax_year": s.tax_year,
        "filing_status": s.filing_status,
        "state": s.state or "",
        "city": s.city or "",
        "classification": s.classification,
    }


@app.get("/api/tax-summary/pdf/form-8949")
def download_form_8949_pdf(
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    _require_verified_email(db, user)
    summary, engine_txns, _, _ = _build_summary(db, user)
    f8949 = form_8949_preview(engine_txns)
    pdf = pdf_reports.build_form_8949_pdf(f8949, _settings_dict(user.settings))
    return _pdf_response(pdf, f"form-8949-{user.settings.tax_year}.pdf")


@app.get("/api/tax-summary/pdf/schedule-d")
def download_schedule_d_pdf(
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    _require_verified_email(db, user)
    summary, engine_txns, _, _ = _build_summary(db, user)
    sd = schedule_d_preview(engine_txns)
    pdf = pdf_reports.build_schedule_d_pdf(
        sd, _summary_to_dict(summary), _settings_dict(user.settings),
    )
    return _pdf_response(pdf, f"schedule-d-{user.settings.tax_year}.pdf")


@app.get("/api/tax-summary/pdf/schedule-c")
def download_schedule_c_pdf(
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    _require_verified_email(db, user)
    s = user.settings
    if Classification(s.classification) != Classification.DEALER:
        raise HTTPException(
            400,
            "Schedule C only applies to dealer classification. Update your settings or take the classification quiz.",
        )
    summary, engine_txns, _, _ = _build_summary(db, user)
    sale_txns = [t for t in engine_txns if not t.is_donation]
    sc = schedule_c_preview(sale_txns)
    net_se = summary.gross_receipts - summary.total_basis - summary.total_fees
    se_tax = self_employment_tax(net_se, FilingStatus(s.filing_status))
    pdf = pdf_reports.build_schedule_c_pdf(
        sc, se_tax, _summary_to_dict(summary), _settings_dict(s),
    )
    return _pdf_response(pdf, f"schedule-c-{s.tax_year}.pdf")


@app.get("/api/tax-summary/pdf/summary")
def download_tax_summary_pdf(
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    _require_verified_email(db, user)
    s = user.settings
    fs = FilingStatus(s.filing_status)
    cls = Classification(s.classification)
    summary, engine_txns, state_detail, local_detail = _build_summary(db, user)
    summary_d = _summary_to_dict(summary)

    quarterly = quarterly_estimated_tax(
        estimated_annual_tax=summary.estimated_total_tax,
        prior_year_tax=s.prior_year_tax or 0.0,
        prior_year_agi=s.prior_year_agi or 0.0,
        withholding_paid=s.withholding_paid or 0.0,
        year=s.tax_year,
    )
    std_ded = STANDARD_DEDUCTION_2025[fs.value]
    regular_ti = max(0.0, s.ordinary_income_estimate - std_ded)
    amt = amt_calculation(
        regular_taxable_income=regular_ti,
        collectible_lt_gain=max(0.0, summary.net_long_term),
        filing_status=fs,
        regular_tax=summary.federal_income_tax,
    )

    se_tax = None
    qbi = None
    if cls == Classification.DEALER:
        net_se = summary.gross_receipts - summary.total_basis - summary.total_fees
        se_tax = self_employment_tax(net_se, fs)
        ti_before_qbi = max(0.0, s.ordinary_income_estimate + max(0.0, net_se) - std_ded)
        qbi = qbi_deduction(
            qualified_business_income=max(0.0, net_se),
            taxable_income_before_qbi=ti_before_qbi,
            net_capital_gain=0.0,
            filing_status=fs,
            w2_wages_paid=0.0,
            qualified_property_basis=0.0,
            is_sstb=False,
        )

    pdf = pdf_reports.build_tax_summary_pdf(
        summary_d, _settings_dict(s),
        state_detail=state_detail, local_detail=local_detail,
        quarterly=quarterly, amt=amt, se_tax=se_tax, qbi=qbi,
    )
    return _pdf_response(pdf, f"cardtax-summary-{s.tax_year}.pdf")


# ---------------------------------------------------------------------------
# Hobby vs Dealer quiz
# ---------------------------------------------------------------------------


@app.get("/api/quiz/factors")
def quiz_factors():
    return {"factors": HOBBY_QUIZ_FACTORS}


@app.post("/api/quiz/submit")
def quiz_submit(
    body: QuizSubmit,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    classification, score, rationale = classify_from_quiz(body.answers)
    user.settings.classification = classification.value
    user.settings.quiz_answers = json.dumps(body.answers)
    db.commit()
    return {
        "classification": classification.value,
        "score": round(score, 3),
        "rationale": rationale,
        "explanation": {
            "hobby": "Income reported gross on Schedule 1; no expense deductions (TCJA §67(g)). Usually the worst outcome.",
            "investor": "Capital gains treatment on Schedule D. Long-term collectible gains capped at 28%.",
            "dealer": "Schedule C trade-or-business — all expenses deductible, but profit is subject to self-employment tax (15.3%).",
        }[classification.value],
    }


# ---------------------------------------------------------------------------
# Break spot allocator (mini-tool)
# ---------------------------------------------------------------------------


@app.post("/api/tools/break-allocate")
def api_break_allocate(
    body: BreakAllocationRequest, user: User = Depends(current_user),
):
    cards = [{"name": c.name, "fmv": c.fmv} for c in body.cards]
    allocations = allocate_break_spot(body.spot_price, cards)
    return {
        "spot_price": body.spot_price,
        "total_fmv": round(sum(c["fmv"] for c in cards), 2),
        "allocations": allocations,
    }


# ---------------------------------------------------------------------------
# Marketplace integrations
# ---------------------------------------------------------------------------


PLATFORM_CATALOG = [
    {
        "key": "ebay",
        "name": "eBay",
        "kind": "oauth",
        "description": "Connect your eBay seller account to auto-import paid orders, fees, and buyer-state info for sales tax nexus.",
        "guide_url": "https://www.ebay.com/sh/ovw",
    },
    {
        "key": "whatnot",
        "name": "Whatnot",
        "kind": "csv",
        "description": "Whatnot has no public API. Export your sales report from the seller dashboard and upload the CSV.",
        "guide_url": "https://help.whatnot.com/hc/en-us/articles/4416977253531",
        "guide": [
            "Open the Whatnot Seller Hub on desktop.",
            "Go to Reports → Sales Report.",
            "Select the date range you want to import.",
            "Click 'Export CSV' and save the file.",
            "Upload it on the Import page (or here).",
        ],
    },
    {
        "key": "comc",
        "name": "COMC",
        "kind": "csv",
        "description": "Check Out My Cards exports a consignment sales report — upload it here for parsing.",
        "guide_url": "https://www.comc.com/Help",
        "guide": [
            "Sign in to comc.com.",
            "Open My Account → Reports → Sales History.",
            "Choose your date range and click 'Download CSV'.",
            "Upload the file here.",
        ],
    },
    {
        "key": "myslabs",
        "name": "MySlabs",
        "kind": "csv",
        "description": "MySlabs doesn't publish an API. Export your sold listings as CSV and upload here.",
        "guide_url": "https://myslabs.com/help",
        "guide": [
            "Log in to myslabs.com.",
            "Open Seller Hub → My Listings → Sold.",
            "Use 'Export to CSV' for the date range you need.",
            "Upload the file here.",
        ],
    },
    {
        "key": "tcgplayer",
        "name": "TCGplayer",
        "kind": "csv",
        "description": "Upload a TCGplayer Seller Portal order export. (TCGplayer's API is invite-only for high-volume sellers.)",
        "guide_url": "https://help.tcgplayer.com/",
        "guide": [
            "Open the TCGplayer Seller Portal.",
            "Go to Orders → Export.",
            "Pick 'All Completed Orders' for your date range.",
            "Download the CSV and upload it here.",
        ],
    },
    {
        "key": "mercari",
        "name": "Mercari",
        "kind": "csv",
        "description": "Mercari emails a sales report to sellers — request and upload it as CSV.",
        "guide_url": "https://www.mercari.com/help_center/",
        "guide": [
            "Open Mercari → Profile → Sales report.",
            "Tap 'Email me the report'.",
            "Save the attached CSV from the email.",
            "Upload it here.",
        ],
    },
    {
        "key": "facebook",
        "name": "Facebook Marketplace",
        "kind": "manual",
        "description": "Facebook Marketplace and FB Groups don't export sales. Add transactions manually on the Transactions page, or use the generic CSV template.",
        "guide_url": "",
        "guide": [
            "On the Transactions page, click 'Add transaction'.",
            "Enter title, sale date, sale price, and any fees Facebook withheld.",
            "Or batch-add via a generic CSV on the Import page.",
        ],
    },
]


def _connection_dict(c: MarketplaceConnection) -> dict:
    return {
        "id": c.id,
        "platform": c.platform,
        "account_label": c.account_label or "",
        "status": c.status or "connected",
        "last_sync_at": c.last_sync_at.isoformat() if c.last_sync_at else None,
        "last_sync_count": c.last_sync_count or 0,
        "last_sync_error": c.last_sync_error or "",
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _platform_summary(db: Session, user_id: int) -> dict[str, dict]:
    """Aggregate transaction counts per platform."""
    out: dict[str, dict] = {}
    rows = (
        db.query(Transaction.platform_source)
        .filter(Transaction.user_id == user_id, Transaction.deleted_at.is_(None))
        .all()
    )
    for (src,) in rows:
        key = (src or "manual").lower()
        out[key] = out.get(key, {"count": 0})
        out[key]["count"] += 1
    return out


@app.get("/api/integrations")
def integrations_list(
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    conns = (
        db.query(MarketplaceConnection)
        .filter(MarketplaceConnection.user_id == user.id)
        .all()
    )
    conn_by_platform = {c.platform: c for c in conns}
    counts = _platform_summary(db, user.id)
    platforms = []
    for p in PLATFORM_CATALOG:
        c = conn_by_platform.get(p["key"])
        platforms.append({
            **p,
            "connection": _connection_dict(c) if c else None,
            "transaction_count": counts.get(p["key"], {}).get("count", 0),
        })
    return {
        "platforms": platforms,
        "ebay_configured": ebay_mod.is_configured(),
    }


@app.get("/api/integrations/history")
def integrations_history(
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    rows = (
        db.query(ImportLog)
        .filter(ImportLog.user_id == user.id)
        .order_by(ImportLog.started_at.desc())
        .limit(50)
        .all()
    )
    return {
        "history": [
            {
                "id": r.id,
                "platform": r.platform,
                "source": r.source,
                "created": r.created,
                "skipped": r.skipped,
                "status": r.status,
                "message": r.message or "",
                "started_at": r.started_at.isoformat() if r.started_at else None,
            }
            for r in rows
        ]
    }


# --- eBay OAuth ----------------------------------------------------------


@app.get("/api/integrations/ebay/start")
def ebay_oauth_start(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_verified_email(db, user)
    if not ebay_mod.is_configured():
        raise HTTPException(400, "eBay integration is not configured on the server.")
    state = auth_mod.new_oauth_state()
    request.session["ebay_oauth_state"] = state
    return {"url": ebay_mod.authorize_url(state)}


@app.get("/api/integrations/ebay/callback")
async def ebay_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if error:
        return RedirectResponse(url=f"/connections?error={error}", status_code=303)
    if not code:
        return RedirectResponse(url="/connections?error=missing_code", status_code=303)

    expected = request.session.pop("ebay_oauth_state", None)
    if not expected or expected != state:
        return RedirectResponse(url="/connections?error=state_mismatch", status_code=303)

    uid = session_user_id(request)
    if not uid:
        return RedirectResponse(url="/login", status_code=303)
    user = db.query(User).filter(User.id == uid).first()
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    try:
        tokens = await ebay_mod.exchange_code(code)
    except Exception as e:
        return RedirectResponse(
            url=f"/connections?error=ebay_exchange_failed", status_code=303
        )

    expires_in = int(tokens.get("expires_in") or 7200)
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in - 60)

    existing = (
        db.query(MarketplaceConnection)
        .filter(
            MarketplaceConnection.user_id == user.id,
            MarketplaceConnection.platform == "ebay",
        ).first()
    )
    if existing:
        existing.access_token = tokens.get("access_token", "")
        existing.refresh_token = tokens.get("refresh_token") or existing.refresh_token
        existing.token_expires_at = expires_at
        existing.scope = tokens.get("scope", "")
        existing.status = "connected"
        existing.last_sync_error = ""
    else:
        db.add(MarketplaceConnection(
            user_id=user.id,
            platform="ebay",
            account_label="eBay account",
            access_token=tokens.get("access_token", ""),
            refresh_token=tokens.get("refresh_token", ""),
            token_expires_at=expires_at,
            scope=tokens.get("scope", ""),
            status="connected",
        ))
    db.commit()
    return RedirectResponse(url="/connections?connected=ebay", status_code=303)


async def _ebay_active_token(db: Session, conn: MarketplaceConnection) -> str:
    """Return a valid access token, refreshing if expired."""
    now = datetime.utcnow()
    if conn.access_token and conn.token_expires_at and conn.token_expires_at > now:
        return conn.access_token
    if not conn.refresh_token:
        raise HTTPException(401, "eBay session expired — please reconnect.")
    tokens = await ebay_mod.refresh_access_token(conn.refresh_token)
    conn.access_token = tokens.get("access_token", "")
    expires_in = int(tokens.get("expires_in") or 7200)
    conn.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in - 60)
    if tokens.get("refresh_token"):
        conn.refresh_token = tokens["refresh_token"]
    db.commit()
    return conn.access_token


class EbaySyncBody(BaseModel):
    days: int = 90


@app.post("/api/integrations/ebay/sync")
async def ebay_sync(
    body: EbaySyncBody,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_verified_email(db, user)
    conn = (
        db.query(MarketplaceConnection)
        .filter(
            MarketplaceConnection.user_id == user.id,
            MarketplaceConnection.platform == "ebay",
        )
        .first()
    )
    if not conn:
        raise HTTPException(400, "eBay account is not connected.")

    started = datetime.utcnow()
    days = max(1, min(int(body.days or 90), 730))
    since = date.today() - timedelta(days=days)

    log = ImportLog(
        user_id=user.id, platform="ebay", source="api",
        started_at=started, status="ok",
    )
    db.add(log)

    try:
        token = await _ebay_active_token(db, conn)
    except HTTPException as e:
        log.status = "error"
        log.message = str(e.detail)
        conn.status = "expired"
        db.commit()
        raise

    try:
        result = await ebay_mod.fetch_orders(token, since=since)
    except PermissionError:
        log.status = "error"
        log.message = "eBay token rejected; please reconnect."
        conn.status = "expired"
        db.commit()
        raise HTTPException(401, "eBay session expired — please reconnect.")
    except Exception as e:
        log.status = "error"
        log.message = f"eBay API error: {e}"[:400]
        conn.last_sync_error = log.message
        db.commit()
        raise HTTPException(502, log.message)

    enforce_transaction_limit(db, user, additional=len(result.sales))

    # De-dupe against existing eBay rows by (title, sale_date, sale_price)
    existing_pairs = {
        (t.item_title, t.sale_date, round(t.sale_price, 2))
        for t in db.query(Transaction).filter(
            Transaction.user_id == user.id,
            Transaction.platform_source == "ebay",
            Transaction.deleted_at.is_(None),
        ).all()
    }

    created = 0
    skipped = 0
    for s in result.sales:
        key = (s.item_title, s.sale_date, round(s.sale_price, 2))
        if key in existing_pairs:
            skipped += 1
            continue
        existing_pairs.add(key)
        db.add(Transaction(
            user_id=user.id,
            item_title=s.item_title,
            sale_date=s.sale_date,
            sale_price=s.sale_price,
            platform_fees=s.platform_fees,
            shipping_charged=s.shipping_charged,
            shipping_cost_out=s.shipping_cost_out,
            platform_source="ebay",
            notes=s.notes,
        ))
        created += 1

    conn.last_sync_at = datetime.utcnow()
    conn.last_sync_count = created
    conn.last_sync_error = ""
    conn.status = "connected"

    log.created = created
    log.skipped = skipped
    log.message = f"Pulled {result.raw_orders} orders → {created} imported, {skipped} duplicates"

    db.commit()
    return {
        "created": created,
        "skipped": skipped,
        "raw_orders": result.raw_orders,
        "message": log.message,
    }


@app.delete("/api/integrations/{platform}")
def disconnect_platform(
    platform: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    platform = platform.lower()
    conn = (
        db.query(MarketplaceConnection)
        .filter(
            MarketplaceConnection.user_id == user.id,
            MarketplaceConnection.platform == platform,
        )
        .first()
    )
    if not conn:
        raise HTTPException(404, "not connected")
    db.delete(conn)
    db.commit()
    return {"ok": True}


# --- Marketplace CSV import (logs to ImportLog) --------------------------


@app.post("/api/integrations/csv-import")
async def integrations_csv_import(
    file: UploadFile = File(...),
    platform: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """CSV import for a *specific* marketplace (COMC, MySlabs, etc.).

    Differs from /api/upload/import by always tagging platform_source and
    writing an ImportLog row that surfaces in the Integrations history.
    """
    _require_verified_email(db, user)
    platform = (platform or "").lower().strip()
    valid = {p["key"] for p in PLATFORM_CATALOG}
    if platform not in valid:
        raise HTTPException(400, "Unknown platform.")

    text_data = (await file.read()).decode("utf-8", errors="replace")
    sales, _ = csv_parsers.parse_csv(text_data, platform_hint=platform)

    enforce_transaction_limit(db, user, additional=len(sales))

    log = ImportLog(user_id=user.id, platform=platform, source="csv", status="ok")
    db.add(log)

    # De-dupe by (sale_date, item_title, sale_price, platform).
    existing = {
        (t.sale_date, (t.item_title or "").strip().lower(),
         round(t.sale_price or 0.0, 2))
        for t in db.query(Transaction).filter(
            Transaction.user_id == user.id,
            Transaction.platform_source == platform,
        ).all()
    }

    created = 0
    skipped = 0
    for s in sales:
        key = (s.sale_date, (s.item_title or "").strip().lower(),
               round(s.sale_price or 0.0, 2))
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        db.add(Transaction(
            user_id=user.id,
            item_title=s.item_title,
            sale_date=s.sale_date,
            sale_price=s.sale_price,
            platform_fees=s.platform_fees,
            shipping_charged=s.shipping_charged,
            shipping_cost_out=s.shipping_cost_out,
            platform_source=platform,
            notes=s.notes or "",
        ))
        created += 1

    log.created = created
    log.skipped = skipped
    if created == 0 and skipped == 0:
        log.status = "error"
        log.message = "No rows parsed. Check the file format or use the generic Import page."
    else:
        dup_msg = f" ({skipped} duplicates skipped)" if skipped else ""
        log.message = f"Imported {created} rows from {platform} CSV{dup_msg}"
    db.commit()
    return {"created": created, "skipped": skipped, "platform": platform, "message": log.message}


# ---------------------------------------------------------------------------
# SEO + icon files served from /static, plus root-level fallbacks
# ---------------------------------------------------------------------------


@app.get("/favicon.ico")
def root_favicon():
    return FileResponse(FRONTEND / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/favicon.svg")
def root_favicon_svg():
    return FileResponse(FRONTEND / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/apple-touch-icon.png")
def root_apple_touch_icon():
    return FileResponse(FRONTEND / "static" / "apple-touch-icon.png", media_type="image/png")


PUBLIC_PATHS = ["/", "/pricing", "/terms", "/privacy", "/login", "/signup"]


def _site_origin(request: Request) -> str:
    """Detect public origin, honoring proxy headers (Railway, Cloudflare, etc)."""
    env_url = os.environ.get("CARDTAX_PUBLIC_URL", "").rstrip("/")
    if env_url:
        return env_url
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt(request: Request):
    origin = _site_origin(request)
    lines = [
        "User-agent: *",
        "Allow: /",
        "Allow: /pricing",
        "Allow: /terms",
        "Allow: /privacy",
        "Disallow: /dashboard",
        "Disallow: /transactions",
        "Disallow: /tax-summary",
        "Disallow: /upload",
        "Disallow: /scan",
        "Disallow: /quiz",
        "Disallow: /tools",
        "Disallow: /settings",
        "Disallow: /account",
        "Disallow: /connections",
        "Disallow: /onboarding",
        "Disallow: /admin",
        "Disallow: /api/",
        "Disallow: /auth/",
        "Disallow: /reset-password",
        "Disallow: /forgot-password",
        "",
        f"Sitemap: {origin}/sitemap.xml",
        "",
    ]
    return "\n".join(lines)


@app.get("/sitemap.xml")
def sitemap_xml(request: Request):
    origin = _site_origin(request)
    today = date.today().isoformat()
    urls = [
        ("/",        "1.0", "weekly"),
        ("/pricing", "0.8", "weekly"),
        ("/terms",   "0.3", "yearly"),
        ("/privacy", "0.3", "yearly"),
        ("/login",   "0.4", "monthly"),
        ("/signup",  "0.6", "monthly"),
    ]
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, priority, changefreq in urls:
        parts.append("  <url>")
        parts.append(f"    <loc>{origin}{path}</loc>")
        parts.append(f"    <lastmod>{today}</lastmod>")
        parts.append(f"    <changefreq>{changefreq}</changefreq>")
        parts.append(f"    <priority>{priority}</priority>")
        parts.append("  </url>")
    parts.append("</urlset>")
    return Response(content="\n".join(parts), media_type="application/xml")


# ---------------------------------------------------------------------------
# Site config (env-driven knobs the frontend wants to know about)
# ---------------------------------------------------------------------------


GOOGLE_ANALYTICS_ID = os.environ.get("GOOGLE_ANALYTICS_ID", "").strip()


@app.get("/api/site-config")
def api_site_config():
    """Public config for the JS bootstrap (analytics IDs, feature flags, etc.)."""
    return {
        "google_analytics_id": GOOGLE_ANALYTICS_ID,
        "sendgrid_configured": email_service.is_configured(),
    }


# ---------------------------------------------------------------------------
# Chat widget — KB auto-answer + escalate-to-human
# ---------------------------------------------------------------------------


class ChatSendBody(BaseModel):
    message: str
    # Hint from the UI: user clicked a suggested question (key) — skip matcher.
    suggestion_key: Optional[str] = None


class ChatEscalateBody(BaseModel):
    message: str
    email: Optional[str] = None  # captured for follow-up; defaults to user.email


class ChatMarkReadBody(BaseModel):
    last_id: int  # mark every message up to and including this one as read


def _chat_msg_out(m: ChatMessage) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "matched_kb_key": m.matched_kb_key or "",
        "needs_human": bool(m.needs_human),
        "answered": bool(m.answered),
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@app.get("/api/chat/history")
def chat_history(user: User = Depends(current_user), db: Session = Depends(get_db)):
    msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .all()
    )
    unread = sum(
        1 for m in msgs
        if m.role == "admin" and not m.read_by_user
    )
    return {
        "messages": [_chat_msg_out(m) for m in msgs],
        "unread_admin_replies": unread,
        "suggestions": chat_kb.list_entries(),
    }


@app.post("/api/chat/send")
def chat_send(
    body: ChatSendBody,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(400, "Message is empty.")
    if len(text) > 2000:
        raise HTTPException(400, "Message is too long (max 2000 characters).")

    # Look up a KB answer. If the UI passed a suggestion_key, prefer that —
    # the user explicitly tapped a canned question.
    entry = None
    confidence = 0.0
    if body.suggestion_key:
        entry = chat_kb.get(body.suggestion_key)
        confidence = 1.0 if entry else 0.0
    if entry is None:
        hit = chat_kb.match(text)
        if hit:
            entry = hit.entry
            confidence = hit.confidence

    user_msg = ChatMessage(
        user_id=user.id,
        role="user",
        content=text,
        matched_kb_key=entry.key if entry else "",
        needs_human=entry is None,
        read_by_user=True,
        read_by_admin=False,
    )
    db.add(user_msg)
    db.flush()

    out_messages = [_chat_msg_out(user_msg)]
    if entry is not None:
        bot_msg = ChatMessage(
            user_id=user.id,
            role="bot",
            content=entry.answer,
            matched_kb_key=entry.key,
            needs_human=False,
            answered=True,
            read_by_user=True,
            read_by_admin=True,
        )
        db.add(bot_msg)
        db.flush()
        out_messages.append(_chat_msg_out(bot_msg))

    db.commit()
    return {
        "messages": out_messages,
        "matched": entry is not None,
        "confidence": round(confidence, 3),
    }


@app.post("/api/chat/escalate")
def chat_escalate(
    body: ChatEscalateBody,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(400, "Message is empty.")
    if len(text) > 2000:
        raise HTTPException(400, "Message is too long (max 2000 characters).")
    email = (body.email or user.email or "").strip().lower()

    user_msg = ChatMessage(
        user_id=user.id,
        role="user",
        content=text,
        contact_email=email,
        needs_human=True,
        read_by_user=True,
        read_by_admin=False,
    )
    db.add(user_msg)
    db.flush()

    # Acknowledgement bot message so the user sees something happen.
    ack = ChatMessage(
        user_id=user.id,
        role="bot",
        content=(
            "Thanks — your question is on its way to the team. We'll reply "
            "right here in chat, usually within one business day. "
            "You'll see a notification dot when there's a response."
        ),
        needs_human=False,
        answered=True,
        read_by_user=True,
        read_by_admin=True,
    )
    db.add(ack)
    db.commit()
    return {"messages": [_chat_msg_out(user_msg), _chat_msg_out(ack)]}


@app.post("/api/chat/mark-read")
def chat_mark_read(
    body: ChatMarkReadBody,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    (
        db.query(ChatMessage)
        .filter(
            ChatMessage.user_id == user.id,
            ChatMessage.id <= body.last_id,
            ChatMessage.role == "admin",
            ChatMessage.read_by_user.is_(False),
        )
        .update({"read_by_user": True}, synchronize_session=False)
    )
    db.commit()
    return {"ok": True}


# --- Admin side -----------------------------------------------------------


class AdminReplyBody(BaseModel):
    content: str


class AdminMarkBody(BaseModel):
    read: bool


def _admin_msg_out(m: ChatMessage, user_email: str) -> dict:
    return {
        "id": m.id,
        "user_id": m.user_id,
        "user_email": user_email,
        "role": m.role,
        "content": m.content,
        "contact_email": m.contact_email or "",
        "matched_kb_key": m.matched_kb_key or "",
        "needs_human": bool(m.needs_human),
        "answered": bool(m.answered),
        "read_by_admin": bool(m.read_by_admin),
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@app.get("/api/admin/messages")
def admin_messages_list(
    request: Request,
    db: Session = Depends(get_db),
    filter: str = "all",  # all | unanswered | unread
):
    _require_admin(request, db)

    # Pull everything, then group by user thread so the admin sees full
    # conversations and not isolated lines.
    q = db.query(ChatMessage).order_by(ChatMessage.created_at.desc())
    rows = q.limit(2000).all()
    user_ids = {r.user_id for r in rows}
    users = {
        u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()
    } if user_ids else {}

    # Group by user_id, build threads in chronological order.
    by_user: dict[int, list[ChatMessage]] = {}
    for r in rows:
        by_user.setdefault(r.user_id, []).append(r)
    for uid in by_user:
        by_user[uid].sort(key=lambda m: (m.created_at or datetime.min, m.id))

    threads = []
    for uid, msgs in by_user.items():
        last_user_msg = next((m for m in reversed(msgs) if m.role == "user"), None)
        unread = sum(1 for m in msgs if m.role == "user" and not m.read_by_admin)
        needs_human = any(m.needs_human and not m.answered for m in msgs if m.role == "user")
        user_email = users.get(uid).email if users.get(uid) else ""

        if filter == "unanswered" and not needs_human:
            continue
        if filter == "unread" and unread == 0:
            continue

        threads.append({
            "user_id": uid,
            "user_email": user_email,
            "message_count": len(msgs),
            "unread_admin_count": unread,
            "needs_human": needs_human,
            "last_at": (last_user_msg.created_at.isoformat()
                        if last_user_msg and last_user_msg.created_at else None),
            "last_preview": (last_user_msg.content[:180] if last_user_msg else ""),
            "messages": [_admin_msg_out(m, user_email) for m in msgs],
        })

    # Sort threads: needs_human first, then unread count, then most recent.
    threads.sort(
        key=lambda t: (
            not t["needs_human"],
            -t["unread_admin_count"],
            t["last_at"] or "",
        ),
        reverse=False,
    )
    # Fix the "most recent" tiebreak which the previous sort flipped.
    threads.sort(
        key=lambda t: t["last_at"] or "",
        reverse=True,
    )
    threads.sort(
        key=lambda t: (t["needs_human"], t["unread_admin_count"]),
        reverse=True,
    )

    counts = {
        "total_threads": len(by_user),
        "unanswered_threads": sum(
            1 for uid, msgs in by_user.items()
            if any(m.needs_human and not m.answered for m in msgs if m.role == "user")
        ),
        "unread_user_messages": sum(
            1 for r in rows if r.role == "user" and not r.read_by_admin
        ),
    }
    return {"threads": threads, "counts": counts}


@app.post("/api/admin/messages/{user_id}/reply")
def admin_messages_reply(
    user_id: int,
    body: AdminReplyBody,
    request: Request,
    db: Session = Depends(get_db),
):
    _require_admin(request, db)
    text = (body.content or "").strip()
    if not text:
        raise HTTPException(400, "Reply is empty.")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(404, "user not found")

    reply = ChatMessage(
        user_id=user_id,
        role="admin",
        content=text,
        needs_human=False,
        answered=True,
        read_by_user=False,
        read_by_admin=True,
    )
    db.add(reply)

    # Mark every prior unanswered user question in this thread as answered +
    # read-by-admin, so it drops off the queue.
    (
        db.query(ChatMessage)
        .filter(
            ChatMessage.user_id == user_id,
            ChatMessage.role == "user",
        )
        .update(
            {"answered": True, "read_by_admin": True},
            synchronize_session=False,
        )
    )
    db.commit()
    db.refresh(reply)
    return {"message": _admin_msg_out(reply, target.email or "")}


@app.post("/api/admin/messages/{user_id}/mark")
def admin_messages_mark(
    user_id: int,
    body: AdminMarkBody,
    request: Request,
    db: Session = Depends(get_db),
):
    _require_admin(request, db)
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(404, "user not found")
    (
        db.query(ChatMessage)
        .filter(
            ChatMessage.user_id == user_id,
            ChatMessage.role == "user",
        )
        .update(
            {"read_by_admin": bool(body.read)},
            synchronize_session=False,
        )
    )
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Waitlist (landing-page email capture, public)
# ---------------------------------------------------------------------------


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_VALID_PLATFORMS = {
    "ebay", "whatnot", "comc", "facebook", "mercari", "tcgplayer",
    "shows", "instagram", "other",
}
_VALID_VOLUMES = {"", "<50", "50-200", "200-1000", "1000+"}


def _require_admin(request: Request, db: Optional[Session] = None) -> None:
    """Compat shim around security.require_admin. Callers that didn't
    previously take `db` get a fresh session."""
    if db is None:
        db = SessionLocal()
        try:
            security_mod.require_admin(request, db)
        finally:
            db.close()
    else:
        security_mod.require_admin(request, db)


class AdminLoginBody(BaseModel):
    email: str
    password: str


@app.get("/admin/login")
def page_admin_login():
    f = FRONTEND / "templates" / "admin_login.html"
    if f.exists():
        return FileResponse(f)
    raise HTTPException(404, "admin login page not built")


@app.get("/api/admin/audit-log")
def api_admin_audit_log(
    request: Request,
    db: Session = Depends(get_db),
    action: Optional[str] = None,
    user_id: Optional[int] = None,
    limit: int = 200,
):
    """Return the most recent audit-log entries. Filterable by action and user.
    Admin-only."""
    _require_admin(request, db)
    limit = max(1, min(limit, 1000))
    q = db.query(AuditLog).order_by(AuditLog.timestamp.desc())
    if action:
        q = q.filter(AuditLog.action == action)
    if user_id is not None:
        q = q.filter(AuditLog.user_id == user_id)
    rows = q.limit(limit).all()
    user_ids = {r.user_id for r in rows if r.user_id}
    users = {
        u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()
    } if user_ids else {}
    return {
        "count": len(rows),
        "entries": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "user_email": users.get(r.user_id).email if users.get(r.user_id) else "",
                "action": r.action,
                "details": r.details or "",
                "ip_address": r.ip_address or "",
                "user_agent": r.user_agent or "",
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            }
            for r in rows
        ],
    }


@app.post("/api/admin/login")
def api_admin_login(
    body: AdminLoginBody, request: Request, db: Session = Depends(get_db),
):
    ratelimit.check(
        request, "admin.login", limit=10, window_seconds=600,
        message="Too many admin sign-in attempts. Please wait a few minutes.",
    )
    email = (body.email or "").strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(body.password or "", user.password_hash):
        raise HTTPException(401, "Invalid credentials.")
    if not security_mod.is_admin_user(db, user):
        raise HTTPException(403, "This account is not an administrator.")
    ensure_settings(db, user)
    login_user(request, user)
    security_mod.write_audit(db, "admin_login", user_id=user.id, request=request)
    return {"ok": True}


@app.post("/api/waitlist")
def waitlist_signup(body: WaitlistIn, request: Request, db: Session = Depends(get_db)):
    email = (body.email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "Please enter a valid email address.")

    platforms = [p for p in (body.platforms or []) if p in _VALID_PLATFORMS]
    volume = body.monthly_volume if body.monthly_volume in _VALID_VOLUMES else ""

    existing = db.query(WaitlistEntry).filter(WaitlistEntry.email == email).first()
    if existing:
        if platforms:
            existing.platforms = ",".join(platforms)
        if volume:
            existing.monthly_volume = volume
        db.commit()
        return {"ok": True, "already_subscribed": True}

    entry = WaitlistEntry(
        email=email,
        platforms=",".join(platforms),
        monthly_volume=volume,
        referrer=(body.referrer or "")[:500],
        user_agent=(request.headers.get("user-agent") or "")[:500],
    )
    db.add(entry)
    db.commit()
    return {"ok": True, "already_subscribed": False}


@app.get("/api/waitlist")
def waitlist_list(request: Request, db: Session = Depends(get_db)):
    _require_admin(request, db)
    rows = db.query(WaitlistEntry).order_by(WaitlistEntry.signup_date.desc()).all()
    return {
        "count": len(rows),
        "entries": [
            {
                "id": r.id,
                "email": r.email,
                "platforms": [p for p in (r.platforms or "").split(",") if p],
                "monthly_volume": r.monthly_volume or "",
                "referrer": r.referrer or "",
                "signup_date": r.signup_date.isoformat() if r.signup_date else None,
            }
            for r in rows
        ],
    }


@app.get("/api/waitlist.csv")
def waitlist_csv(request: Request, db: Session = Depends(get_db)):
    _require_admin(request, db)
    rows = db.query(WaitlistEntry).order_by(WaitlistEntry.signup_date.desc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "platforms", "monthly_volume", "signup_date", "referrer"])
    for r in rows:
        w.writerow([
            r.email,
            r.platforms or "",
            r.monthly_volume or "",
            r.signup_date.isoformat() if r.signup_date else "",
            r.referrer or "",
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="cardtax-waitlist.csv"'},
    )


# ---------------------------------------------------------------------------
# Card scanner (Coming soon — gated by CARDTAX_AI_SCANNER_ENABLED)
# ---------------------------------------------------------------------------

# Flip this env var (or set the constant below) to "1" to bring the AI scanner
# back online once the OpenAI integration is ready to ship to users.
AI_SCANNER_ENABLED = os.environ.get("CARDTAX_AI_SCANNER_ENABLED", "").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


@app.get("/api/scan/status")
def scan_status(user: User = Depends(current_user)):
    """Lets the frontend know whether AI scanning is configured + plan-gated."""
    if not AI_SCANNER_ENABLED:
        return {
            "ai_available": False,
            "model": card_scanner.DEFAULT_MODEL,
            "feature_unlocked": False,
            "tier": user.subscription_tier or "free",
            "coming_soon": True,
        }
    return {
        "ai_available": card_scanner.is_available(),
        "model": card_scanner.DEFAULT_MODEL,
        "feature_unlocked": has_feature(user, "ai_scanner"),
        "tier": user.subscription_tier or "free",
        "coming_soon": False,
    }


@app.post("/api/scan")
async def api_scan(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
):
    if not AI_SCANNER_ENABLED:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "coming_soon",
                "feature": "ai_scanner",
                "message": "The AI card scanner is coming soon. Use manual entry or CSV import for now.",
            },
        )
    if not has_feature(user, "ai_scanner"):
        raise HTTPException(
            status_code=402,
            detail={
                "code": "feature_locked",
                "feature": "ai_scanner",
                "message": "The AI card scanner is included with the Unlimited plan.",
            },
        )
    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(400, "empty file")
    path = card_scanner.save_upload(image_bytes, file.filename or "card.jpg")
    result = card_scanner.identify_card(image_bytes, content_type=file.content_type or "image/jpeg")
    result["image_path"] = "/" + path
    return result
