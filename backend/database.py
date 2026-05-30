"""SQLAlchemy models and DB session for CardTax.

Engine selection:
- If DATABASE_URL is set and starts with "postgresql" (or "postgres://"), use
  PostgreSQL with connection pooling.
- Otherwise fall back to a local SQLite file under ../data/cardtax.db.

Schema migrations are managed by Alembic (see ./alembic.ini and ./alembic/).
``init_db()`` here is a thin wrapper that creates tables on a fresh SQLite
file for the local-dev "just run it" path; Alembic is the source of truth in
production.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import (
    Boolean, Column, Index, Integer, String, Float, Date, DateTime,
    ForeignKey, Text, create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

DB_PATH = Path(__file__).parent.parent / "data" / "cardtax.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _normalize_db_url(url: str) -> str:
    # Heroku/Railway still hand out the legacy "postgres://" scheme; SQLAlchemy
    # 2.x only accepts "postgresql://".
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def _build_engine():
    url = os.environ.get("DATABASE_URL", "").strip()
    if url and (url.startswith("postgresql") or url.startswith("postgres://")):
        url = _normalize_db_url(url)
        return create_engine(
            url,
            pool_size=5,
            max_overflow=0,
            pool_pre_ping=True,
            pool_recycle=1800,
            echo=False,
            future=True,
        )
    # SQLite default (local dev). check_same_thread=False so FastAPI worker
    # threads can share the connection.
    sqlite_url = f"sqlite:///{DB_PATH}"
    return create_engine(
        sqlite_url,
        connect_args={"check_same_thread": False},
        echo=False,
        future=True,
    )


engine = _build_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=True)
    display_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Auth
    password_hash = Column(String, nullable=True)  # bcrypt hash; null = OAuth-only
    google_sub = Column(String, nullable=True, unique=True)  # Google "sub" claim

    # Subscription / billing
    subscription_tier = Column(String, default="free", nullable=False)
    subscription_status = Column(String, default="active", nullable=False)
    stripe_customer_id = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)
    current_period_end = Column(DateTime, nullable=True)

    # Onboarding
    onboarded_at = Column(DateTime, nullable=True)

    # Email notification preferences (security emails like password reset are not toggleable)
    email_pref_tax_deadlines = Column(Boolean, default=True, nullable=False)
    email_pref_subscription = Column(Boolean, default=True, nullable=False)
    email_pref_weekly_digest = Column(Boolean, default=True, nullable=False)
    # Bookkeeping for weekly digest cron — last time we sent one
    last_digest_sent_at = Column(DateTime, nullable=True)
    # Bookkeeping for tax-deadline reminders — last (deadline_label + year) we notified for
    last_deadline_notified = Column(String, default="")

    transactions = relationship("Transaction", back_populates="user", cascade="all, delete-orphan")
    settings = relationship("TaxSettings", back_populates="user", uselist=False, cascade="all, delete-orphan")
    chat_messages = relationship("ChatMessage", cascade="all, delete-orphan")
    password_reset_tokens = relationship("PasswordResetToken", cascade="all, delete-orphan")


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    item_title = Column(String, nullable=False)
    sale_date = Column(Date, nullable=False)
    sale_price = Column(Float, nullable=False, default=0.0)        # gross 1099-K
    platform_fees = Column(Float, nullable=False, default=0.0)
    shipping_charged = Column(Float, nullable=False, default=0.0)   # buyer paid
    shipping_cost_out = Column(Float, nullable=False, default=0.0)  # seller paid out

    purchase_date = Column(Date, nullable=True)
    purchase_price = Column(Float, nullable=False, default=0.0)
    grading_fees = Column(Float, nullable=False, default=0.0)
    other_basis_costs = Column(Float, nullable=False, default=0.0)

    platform_source = Column(String, default="manual", index=True)  # ebay, whatnot, comc, manual
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    image_path = Column(String, default="")
    card_category = Column(String, default="")  # sports / pokemon / mtg / yugioh / other

    # Soft delete — set to a timestamp when the row is "deleted" via API.
    # Queries throughout the app filter on this being NULL.
    deleted_at = Column(DateTime, nullable=True, index=True)

    # Acquisition type and related fields
    acquisition_type = Column(String, default="purchase")
    donor_basis = Column(Float, nullable=True)
    gift_date = Column(Date, nullable=True)
    fmv_at_gift = Column(Float, nullable=True)
    donor_holding_period_start = Column(Date, nullable=True)
    date_of_death = Column(Date, nullable=True)
    fmv_at_death = Column(Float, nullable=True)
    break_spot_price = Column(Float, nullable=True)
    break_total_fmv = Column(Float, nullable=True)
    donation_date = Column(Date, nullable=True)
    fmv_at_donation = Column(Float, nullable=True)
    donee_organization = Column(String, default="")
    donee_unrelated_use = Column(Boolean, default=True)

    user = relationship("User", back_populates="transactions")
    basis_items = relationship(
        "CostBasisItem", back_populates="transaction", cascade="all, delete-orphan"
    )

    @property
    def is_donation(self) -> bool:
        return (self.acquisition_type or "") == "donation"

    @property
    def is_personal_use(self) -> bool:
        return (self.acquisition_type or "") == "personal_collection"

    @property
    def net_proceeds(self) -> float:
        return self.sale_price - self.platform_fees - self.shipping_cost_out

    @property
    def total_basis(self) -> float:
        """Mirrors tax_engine.Transaction.total_basis (gain basis)."""
        extra = sum(b.amount for b in self.basis_items) if self.basis_items else 0.0
        non_purchase_basis = self.grading_fees + self.other_basis_costs + extra
        atype = self.acquisition_type or "purchase"
        if atype == "inheritance":
            return (self.fmv_at_death or 0.0) + non_purchase_basis
        if atype == "gift":
            return (self.donor_basis or 0.0) + non_purchase_basis
        return self.purchase_price + non_purchase_basis

    @property
    def loss_basis(self) -> float:
        atype = self.acquisition_type or "purchase"
        if atype != "gift":
            return self.total_basis
        extra = sum(b.amount for b in self.basis_items) if self.basis_items else 0.0
        non = self.grading_fees + self.other_basis_costs + extra
        if self.fmv_at_gift is None:
            return (self.donor_basis or 0.0) + non
        return min((self.donor_basis or 0.0), self.fmv_at_gift) + non

    @property
    def gain_loss(self) -> float:
        if self.is_donation:
            return 0.0
        proceeds = self.net_proceeds
        atype = self.acquisition_type or "purchase"
        if atype == "gift" and self.fmv_at_gift is not None:
            gain_b = self.total_basis
            loss_b = self.loss_basis
            if proceeds > gain_b:
                return proceeds - gain_b
            if proceeds < loss_b:
                return proceeds - loss_b
            return 0.0
        gl = proceeds - self.total_basis
        if self.is_personal_use and gl < 0:
            return 0.0
        return gl

    @property
    def holding_period_days(self) -> int | None:
        atype = self.acquisition_type or "purchase"
        if atype == "inheritance":
            return 366
        start = self.purchase_date
        if atype == "gift":
            start = self.donor_holding_period_start or self.gift_date or self.purchase_date
        if not start or not self.sale_date:
            return None
        return (self.sale_date - start).days

    @property
    def is_long_term(self) -> bool:
        atype = self.acquisition_type or "purchase"
        if atype == "inheritance":
            return True
        hp = self.holding_period_days
        return hp is not None and hp >= 366


class CostBasisItem(Base):
    __tablename__ = "cost_basis_items"
    id = Column(Integer, primary_key=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)  # grading, shipping_in, insurance, break_spot, packaging, buyer_premium, other
    amount = Column(Float, nullable=False, default=0.0)
    note = Column(String, default="")

    transaction = relationship("Transaction", back_populates="basis_items")


class WaitlistEntry(Base):
    __tablename__ = "waitlist"
    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=False, unique=True, index=True)
    platforms = Column(Text, default="")  # comma-separated list: ebay,whatnot,comc,...
    monthly_volume = Column(String, default="")  # bucket: <50, 50-200, 200-1000, 1000+
    referrer = Column(String, default="")
    user_agent = Column(String, default="")
    signup_date = Column(DateTime, default=datetime.utcnow)


class MarketplaceConnection(Base):
    """OAuth / API credentials per user per marketplace (eBay, etc.)."""
    __tablename__ = "marketplace_connections"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    platform = Column(String, nullable=False)  # ebay, comc, myslabs, ...
    account_label = Column(String, default="")  # display name e.g. eBay username

    access_token = Column(Text, default="")
    refresh_token = Column(Text, default="")
    token_expires_at = Column(DateTime, nullable=True)
    scope = Column(Text, default="")

    last_sync_at = Column(DateTime, nullable=True)
    last_sync_count = Column(Integer, default=0)
    last_sync_error = Column(Text, default="")

    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="connected")  # connected, expired, error


class ChatMessage(Base):
    """One row per message in the in-app chat.

    role:
      - "user"  — message typed by the user
      - "bot"   — auto-reply from the KB matcher
      - "admin" — reply from the site owner

    needs_human is set on a user message that the bot couldn't answer (or that
    the user routed straight to "talk to a person"). The admin queue filters
    on this + answered to decide what still needs a reply.
    """
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String, nullable=False)  # user | bot | admin
    content = Column(Text, nullable=False, default="")
    contact_email = Column(String, default="")  # captured when escalating
    matched_kb_key = Column(String, default="")  # which KB entry the bot used
    needs_human = Column(Boolean, default=False)  # user msg awaiting admin
    answered = Column(Boolean, default=False)     # user msg has admin reply
    read_by_user = Column(Boolean, default=True)  # user has seen this msg
    read_by_admin = Column(Boolean, default=False)  # admin has seen this msg
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class PasswordResetToken(Base):
    """Single-use, time-limited token for the password-reset flow."""
    __tablename__ = "password_reset_tokens"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token = Column(String, nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ImportLog(Base):
    """One row per import attempt — manual CSV, OAuth sync, etc."""
    __tablename__ = "import_logs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    platform = Column(String, nullable=False)
    source = Column(String, default="csv")  # csv, api, manual
    created = Column(Integer, default=0)
    skipped = Column(Integer, default=0)
    status = Column(String, default="ok")  # ok, error
    message = Column(Text, default="")
    started_at = Column(DateTime, default=datetime.utcnow)


class TaxSettings(Base):
    __tablename__ = "tax_settings"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    tax_year = Column(Integer, default=2025)
    filing_status = Column(String, default="single")   # single, mfj, mfs, hoh
    state = Column(String, default="")                  # 2-letter code or ""
    city = Column(String, default="")                    # locality_key like "NY:NYC"
    ordinary_income_estimate = Column(Float, default=0.0)
    classification = Column(String, default="investor")  # hobby, investor, dealer
    lot_method = Column(String, default="fifo")
    prior_year_tax = Column(Float, default=0.0)
    prior_year_agi = Column(Float, default=0.0)
    withholding_paid = Column(Float, default=0.0)
    is_kiddie_filer = Column(Boolean, default=False)
    parent_marginal_rate = Column(Float, default=0.22)
    nol_carryforward = Column(Float, default=0.0)
    quiz_answers = Column(Text, default="{}")  # JSON

    user = relationship("User", back_populates="settings")


def _alembic_config():
    """Build an Alembic Config that mirrors what the CLI would see."""
    from alembic.config import Config

    ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(ini_path))
    # Override sqlalchemy.url with whatever engine we actually built, so the
    # CLI and the in-process call agree even if DATABASE_URL changes shape.
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    return cfg


def init_db():
    """Bootstrap the database via Alembic.

    Three cases:
      1. Fresh database (no tables at all)         -> ``alembic upgrade head``
      2. Pre-Alembic database (tables but no
         ``alembic_version`` row)                  -> stamp at ``0001_initial``,
                                                      then ``upgrade head``
      3. Already-managed database                  -> ``upgrade head`` (no-op
                                                      if at head)

    Production deploys should call ``alembic upgrade head`` directly (the
    Dockerfile does), but this in-process bootstrap keeps ``./start.sh`` a
    one-command dev experience and handles the migration off the old
    ALTER TABLE bootstrap.
    """
    from sqlalchemy import inspect as sa_inspect
    from alembic import command

    insp = sa_inspect(engine)
    has_alembic = insp.has_table("alembic_version")
    has_users = insp.has_table("users")

    cfg = _alembic_config()

    if not has_alembic and has_users:
        # Pre-Alembic dev database: schema is at the 0001 baseline.
        command.stamp(cfg, "0001_initial")
    command.upgrade(cfg, "head")


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_settings(db: Session, user: User) -> User:
    """Make sure a user has a TaxSettings row."""
    if not user.settings:
        db.add(TaxSettings(user_id=user.id))
        db.commit()
        db.refresh(user)
    return user
