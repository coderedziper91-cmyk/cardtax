"""Pytest fixtures for CardTax.

Provides:
  - in-memory SQLite database swapped into backend.database
  - FastAPI TestClient with dependency overrides
  - authenticated_user fixture (creates user + logs in via /api/auth/signup)
  - sample transaction data for tax-engine tests
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

# Make ~/cardtax importable as `backend.*`
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Force a sane session secret so tests are deterministic
os.environ.setdefault("CARDTAX_SESSION_SECRET", "test-secret-32-chars-minimum-padding")
# Prevent SendGrid/stripe network calls during tests
os.environ.pop("STRIPE_SECRET_KEY", None)
os.environ.pop("SENDGRID_API_KEY", None)


# ---------------------------------------------------------------------------
# Database: in-memory SQLite, swapped into backend.database
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session(monkeypatch, tmp_path):
    """Provide a fresh on-disk SQLite db per test.

    We create the schema with SQLAlchemy's ORM metadata, then stamp Alembic
    at HEAD so the app's ``init_db()`` (called from FastAPI startup) becomes
    a no-op instead of trying to ALTER TABLE columns that already exist.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend import database as db_mod

    test_db_path = tmp_path / "cardtax_test.db"
    # Point DATABASE_URL at the test DB so Alembic env.py picks the same one
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{test_db_path}")

    test_engine = create_engine(
        f"sqlite:///{test_db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    monkeypatch.setattr(db_mod, "engine", test_engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSessionLocal)

    # Build the schema from ORM metadata then stamp Alembic at head so the
    # app's startup-time alembic upgrade is a no-op (avoids ALTER TABLE
    # duplicate-column errors).
    db_mod.Base.metadata.create_all(bind=test_engine)
    from alembic import command
    cfg = db_mod._alembic_config()
    command.stamp(cfg, "head")

    sess = TestSessionLocal()
    try:
        yield sess
    finally:
        sess.close()
        test_engine.dispose()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """The rate limiter is module-level state — clear it between tests so
    repeated /api/auth/forgot / /api/auth/login calls don't accumulate."""
    from backend import ratelimit
    ratelimit._buckets.clear()
    ratelimit.reset_global()
    yield
    ratelimit._buckets.clear()
    ratelimit.reset_global()


@pytest.fixture
def client(db_session, monkeypatch):
    """FastAPI TestClient with the test DB injected via get_db override."""
    from fastapi.testclient import TestClient

    from backend import database as db_mod
    from backend.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[db_mod.get_db] = override_get_db
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Test users
# ---------------------------------------------------------------------------


@pytest.fixture
def user_factory(db_session):
    """Returns a callable that creates a User row directly in the test DB.

    Users are created with email already verified so they can access the
    PDF download / CSV upload / sync endpoints that gate on verification.
    """
    from backend import auth as auth_mod
    from backend import security as security_mod
    from backend.database import TaxSettings, User

    counter = {"n": 0}

    def make(
        email: str | None = None,
        password: str = "SuperSecret1",
        tier: str = "free",
        verified: bool = True,
    ):
        counter["n"] += 1
        email = email or f"user{counter['n']}@example.com"
        u = User(
            email=email,
            display_name=email.split("@")[0],
            password_hash=auth_mod.hash_password(password),
            subscription_tier=tier,
            subscription_status="active",
        )
        db_session.add(u)
        db_session.flush()
        db_session.add(TaxSettings(user_id=u.id))
        db_session.commit()
        db_session.refresh(u)
        if verified:
            security_mod.mark_email_verified(db_session, u)
        return u

    return make


@pytest.fixture
def authenticated_client(client, user_factory):
    """A TestClient whose session cookie is already populated with a logged-in user."""
    user = user_factory(email="auth@example.com", password="SuperSecret1")
    resp = client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "SuperSecret1"},
    )
    assert resp.status_code == 200, resp.text
    return client, user


# ---------------------------------------------------------------------------
# Sample tax-engine objects
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_txn_factory():
    """Returns a callable that builds tax_engine.Transaction objects with sane defaults."""
    from backend.tax_engine import AcquisitionType, CostBasisComponent, Transaction

    def make(
        *,
        id=None,
        item_title="Test Card",
        sale_date=date(2025, 6, 1),
        sale_price=200.0,
        platform_fees=20.0,
        shipping_charged=0.0,
        shipping_cost_out=0.0,
        purchase_date=date(2024, 1, 1),
        purchase_price=100.0,
        basis_components=None,
        platform_source="ebay",
        acquisition_type=AcquisitionType.PURCHASE,
        **kwargs,
    ):
        return Transaction(
            id=id,
            item_title=item_title,
            sale_date=sale_date,
            sale_price=sale_price,
            platform_fees=platform_fees,
            shipping_charged=shipping_charged,
            shipping_cost_out=shipping_cost_out,
            purchase_date=purchase_date,
            purchase_price=purchase_price,
            basis_components=basis_components or [],
            platform_source=platform_source,
            acquisition_type=acquisition_type,
            **kwargs,
        )

    return make
