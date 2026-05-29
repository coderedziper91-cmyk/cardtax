"""Subscription enforcement: tier limits, upgrades, feature gates."""

from __future__ import annotations

from datetime import date

import pytest

from backend.billing import (
    TIERS,
    enforce_transaction_limit,
    has_feature,
    tier_info,
    transaction_count_for_year,
    yearly_limit,
)


# ---------------------------------------------------------------------------
# Tier metadata
# ---------------------------------------------------------------------------


class TestTiers:
    def test_free_tier_25_transactions(self):
        assert yearly_limit("free") == 25

    def test_starter_tier_200_transactions(self):
        assert yearly_limit("starter") == 200

    def test_pro_tier_1000_transactions(self):
        assert yearly_limit("pro") == 1_000

    def test_unlimited_tier_no_limit(self):
        assert yearly_limit("unlimited") is None

    def test_tier_info_returns_dict(self):
        info = tier_info("pro")
        assert info["key"] == "pro"
        assert "price_monthly" in info


# ---------------------------------------------------------------------------
# Limit enforcement
# ---------------------------------------------------------------------------


def _add_n_transactions(db, user, n):
    """Add n bare transactions for the user."""
    from backend.database import Transaction
    for i in range(n):
        db.add(Transaction(
            user_id=user.id,
            item_title=f"Card {i}",
            sale_date=date(date.today().year, 1, 1),
            sale_price=10.0,
        ))
    db.commit()


class TestLimitEnforcement:
    def test_free_25th_succeeds(self, db_session, user_factory):
        u = user_factory(tier="free")
        _add_n_transactions(db_session, u, 24)
        # Adding 1 more (the 25th) should NOT raise
        enforce_transaction_limit(db_session, u, additional=1)

    def test_free_26th_blocked(self, db_session, user_factory):
        from fastapi import HTTPException
        u = user_factory(tier="free")
        _add_n_transactions(db_session, u, 25)
        with pytest.raises(HTTPException) as exc:
            enforce_transaction_limit(db_session, u, additional=1)
        assert exc.value.status_code == 402

    def test_starter_200th_succeeds(self, db_session, user_factory):
        u = user_factory(tier="starter")
        _add_n_transactions(db_session, u, 199)
        enforce_transaction_limit(db_session, u, additional=1)

    def test_starter_201st_blocked(self, db_session, user_factory):
        from fastapi import HTTPException
        u = user_factory(tier="starter")
        _add_n_transactions(db_session, u, 200)
        with pytest.raises(HTTPException) as exc:
            enforce_transaction_limit(db_session, u, additional=1)
        assert exc.value.status_code == 402

    def test_pro_1000th_succeeds(self, db_session, user_factory):
        u = user_factory(tier="pro")
        _add_n_transactions(db_session, u, 999)
        enforce_transaction_limit(db_session, u, additional=1)

    def test_pro_1001st_blocked(self, db_session, user_factory):
        from fastapi import HTTPException
        u = user_factory(tier="pro")
        _add_n_transactions(db_session, u, 1_000)
        with pytest.raises(HTTPException) as exc:
            enforce_transaction_limit(db_session, u, additional=1)
        assert exc.value.status_code == 402

    def test_unlimited_no_limit(self, db_session, user_factory):
        u = user_factory(tier="unlimited")
        _add_n_transactions(db_session, u, 100)  # generous
        # Should never raise no matter the count
        enforce_transaction_limit(db_session, u, additional=1_000_000)

    def test_tier_upgrade_unlocks(self, db_session, user_factory):
        # Start free, fill quota → upgrade to starter → can add more
        from fastapi import HTTPException
        u = user_factory(tier="free")
        _add_n_transactions(db_session, u, 25)
        # Free is full
        with pytest.raises(HTTPException):
            enforce_transaction_limit(db_session, u, additional=1)
        # Upgrade
        u.subscription_tier = "starter"
        db_session.commit()
        # Now we can add more (starter has room for 175 more)
        enforce_transaction_limit(db_session, u, additional=1)


# ---------------------------------------------------------------------------
# Feature gates (AI scanner, Form 8949 export)
# ---------------------------------------------------------------------------


class TestFeatureGates:
    def test_ai_scanner_requires_unlimited(self, user_factory):
        for tier in ["free", "starter", "pro"]:
            u = user_factory(tier=tier)
            assert has_feature(u, "ai_scanner") is False
        u = user_factory(tier="unlimited")
        assert has_feature(u, "ai_scanner") is True

    def test_form_8949_export_requires_unlimited(self, user_factory):
        u = user_factory(tier="pro")
        assert has_feature(u, "form_8949_export") is False
        u = user_factory(tier="unlimited")
        assert has_feature(u, "form_8949_export") is True

    def test_unknown_feature_is_allowed(self, user_factory):
        # Features that aren't premium-gated should always be allowed
        u = user_factory(tier="free")
        assert has_feature(u, "something_random") is True


# ---------------------------------------------------------------------------
# Counting only the current calendar year
# ---------------------------------------------------------------------------


class TestTransactionCounting:
    def test_only_current_year_counts(self, db_session, user_factory):
        from backend.database import Transaction
        u = user_factory(tier="free")
        # Add 20 transactions in 2020 (out of year)
        for i in range(20):
            db_session.add(Transaction(
                user_id=u.id, item_title=f"Old {i}",
                sale_date=date(2020, 1, 1), sale_price=10.0,
            ))
        # Add 5 in current year
        for i in range(5):
            db_session.add(Transaction(
                user_id=u.id, item_title=f"New {i}",
                sale_date=date(date.today().year, 6, 1), sale_price=10.0,
            ))
        db_session.commit()
        count = transaction_count_for_year(db_session, u.id)
        assert count == 5

    def test_count_for_specific_year(self, db_session, user_factory):
        from backend.database import Transaction
        u = user_factory(tier="pro")
        for i in range(10):
            db_session.add(Transaction(
                user_id=u.id, item_title=f"Card {i}",
                sale_date=date(2023, 6, 1), sale_price=10.0,
            ))
        db_session.commit()
        assert transaction_count_for_year(db_session, u.id, year=2023) == 10
        assert transaction_count_for_year(db_session, u.id, year=2024) == 0


# ---------------------------------------------------------------------------
# API enforcement (end-to-end through /api/transactions)
# ---------------------------------------------------------------------------


class TestAPILimitEnforcement:
    def test_create_transaction_succeeds_under_limit(self, authenticated_client):
        client, _ = authenticated_client
        resp = client.post("/api/transactions", json={
            "item_title": "Test",
            "sale_date": "2025-06-01",
            "sale_price": 100.0,
        })
        assert resp.status_code == 200

    def test_create_transaction_402_at_limit(self, authenticated_client, db_session):
        from backend.database import Transaction, User
        client, user = authenticated_client
        # Fill up to free quota of 25
        for i in range(25):
            db_session.add(Transaction(
                user_id=user.id, item_title=f"F{i}",
                sale_date=date(date.today().year, 1, 1), sale_price=1.0,
            ))
        db_session.commit()
        # 26th via API should be blocked
        resp = client.post("/api/transactions", json={
            "item_title": "Overflow",
            "sale_date": f"{date.today().year}-06-01",
            "sale_price": 10.0,
        })
        assert resp.status_code == 402
