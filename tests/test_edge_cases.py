"""Edge cases: $0, negatives, very large amounts, unicode, long names, date boundaries, empty CSVs."""

from __future__ import annotations

from datetime import date

import pytest

from backend.csv_parsers import parse_csv, parse_ebay
from backend.tax_engine import (
    AcquisitionType,
    Classification,
    FilingStatus,
    Transaction,
    summarize,
)


# ---------------------------------------------------------------------------
# Money amount extremes
# ---------------------------------------------------------------------------


class TestMoneyExtremes:
    def test_zero_sale_amount(self, sample_txn_factory):
        t = sample_txn_factory(
            sale_price=0.0, purchase_price=0.0,
            platform_fees=0.0, shipping_cost_out=0.0,
        )
        assert t.gain_loss == 0.0
        assert t.net_proceeds == 0.0

    def test_negative_amount_return(self, sample_txn_factory):
        # Refund: negative sale price
        t = sample_txn_factory(
            sale_price=-50.0,
            platform_fees=0.0, shipping_cost_out=0.0,
            purchase_price=0.0,
        )
        # Net proceeds = -50, gain = -50
        assert t.net_proceeds == pytest.approx(-50.0)
        assert t.gain_loss == pytest.approx(-50.0)

    def test_very_large_amount(self, sample_txn_factory):
        # $1M+ sale
        t = sample_txn_factory(
            sale_price=1_500_000.0,
            purchase_price=100_000.0,
            platform_fees=150_000.0,
            shipping_cost_out=500.0,
        )
        # Net proceeds = 1,500,000 - 150,000 - 500 = 1,349,500
        # Gain = 1,349,500 - 100,000 = 1,249,500
        assert t.gain_loss == pytest.approx(1_249_500.0)

    def test_summarize_handles_large_gain(self, sample_txn_factory):
        t = sample_txn_factory(
            sale_price=1_500_000.0,
            purchase_price=100_000.0,
            platform_fees=0.0, shipping_cost_out=0.0,
            purchase_date=date(2020, 1, 1),
            sale_date=date(2025, 6, 1),
        )
        s = summarize([t], 80_000, FilingStatus.SINGLE, Classification.INVESTOR)
        # Long-term, should hit 28% cap
        assert s.net_long_term == pytest.approx(1_400_000.0)
        assert s.collectibles_tax > 0


# ---------------------------------------------------------------------------
# Card name edge cases
# ---------------------------------------------------------------------------


class TestCardNames:
    def test_unicode_card_name(self, sample_txn_factory):
        t = sample_txn_factory(item_title="Pokémon Pikachu ⚡ #25 (1st Ed) — Holo")
        assert "Pokémon" in t.item_title
        assert "⚡" in t.item_title

    def test_quotes_in_card_name(self, sample_txn_factory):
        t = sample_txn_factory(item_title='Babe "The Sultan" Ruth 1933 Goudey')
        assert '"The Sultan"' in t.item_title

    def test_ampersand_in_card_name(self, sample_txn_factory):
        t = sample_txn_factory(item_title="Topps & Bowman Combined Edition")
        assert "&" in t.item_title

    def test_very_long_card_name_500_chars(self, sample_txn_factory):
        long_name = "Mickey Mantle " * 50  # ~700 chars
        t = sample_txn_factory(item_title=long_name)
        assert len(t.item_title) > 500


# ---------------------------------------------------------------------------
# Date boundary edge cases
# ---------------------------------------------------------------------------


class TestDateBoundaries:
    def test_dec_31_jan_1_boundary(self, sample_txn_factory):
        # Bought Dec 31, sold Jan 1 of next year → 1 day, short term
        t = sample_txn_factory(
            purchase_date=date(2024, 12, 31),
            sale_date=date(2025, 1, 1),
        )
        assert t.holding_period_days == 1
        assert t.is_long_term is False

    def test_leap_year_feb_29(self, sample_txn_factory):
        # 2024 was a leap year
        t = sample_txn_factory(
            purchase_date=date(2024, 2, 29),
            sale_date=date(2025, 3, 1),
        )
        # 366 days exactly (leap year + 1 day)
        assert t.holding_period_days == 366
        assert t.is_long_term is True

    def test_same_day_buy_sell(self, sample_txn_factory):
        t = sample_txn_factory(
            purchase_date=date(2025, 6, 1),
            sale_date=date(2025, 6, 1),
        )
        assert t.holding_period_days == 0
        assert t.is_long_term is False

    def test_year_boundary_does_not_double_count(self, sample_txn_factory):
        # Two sales on different days, should both appear
        t1 = sample_txn_factory(sale_date=date(2024, 12, 31), sale_price=100, purchase_price=50, platform_fees=0, shipping_cost_out=0)
        t2 = sample_txn_factory(sale_date=date(2025, 1, 1), sale_price=100, purchase_price=50, platform_fees=0, shipping_cost_out=0)
        s = summarize([t1, t2], 50_000, FilingStatus.SINGLE, Classification.INVESTOR)
        assert s.gross_receipts == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# CSV edge cases
# ---------------------------------------------------------------------------


class TestCSVEdgeCases:
    def test_empty_csv(self):
        sales, _ = parse_csv("")
        assert sales == []

    def test_only_headers_no_data(self):
        csv = "Item title,Transaction date,Sold for\n"
        sales = parse_ebay(csv)
        assert sales == []

    def test_csv_with_only_blank_rows(self):
        csv = "Item title,Transaction date,Sold for\n\n\n,,\n"
        sales = parse_ebay(csv)
        assert sales == []

    def test_csv_with_blank_amounts(self):
        csv = (
            "Item title,Transaction date,Sold for,Total fees\n"
            "Card A,2025-06-01,,\n"
        )
        sales = parse_ebay(csv)
        # Should parse but with $0 amounts
        assert len(sales) == 1
        assert sales[0].sale_price == 0.0

    def test_csv_unicode(self):
        csv = (
            "Item title,Transaction date,Sold for,Total fees\n"
            "Pokémon Charizard ★ 1st Ed,2025-06-01,$1000,$100\n"
        )
        sales = parse_ebay(csv)
        assert sales[0].item_title == "Pokémon Charizard ★ 1st Ed"


# ---------------------------------------------------------------------------
# Concurrent uploads from same user
# ---------------------------------------------------------------------------


class TestConcurrentUploads:
    def test_two_sequential_uploads_accumulate(self, authenticated_client):
        """Simulate two CSV uploads from the same user — counts should add up.

        Multipart uploads need a CSRF token in the form body since the
        Content-Type isn't application/json (which is the auto-exempt case).
        """
        client, _ = authenticated_client
        # Get a CSRF token first
        tok_resp = client.get("/api/csrf-token")
        csrf_tok = tok_resp.json().get("csrf_token", "")
        headers = {"x-csrf-token": csrf_tok}

        csv_data = (
            "Item title,Transaction date,Sold for,Total fees\n"
            "Card A,2025-06-01,$100,$10\n"
            "Card B,2025-06-02,$200,$20\n"
        )
        # First upload
        files = {"file": ("export.csv", csv_data, "text/csv")}
        resp1 = client.post(
            "/api/upload/import",
            files=files, data={"platform": "ebay"}, headers=headers,
        )
        assert resp1.status_code == 200, resp1.text
        first_count = resp1.json()["created"]
        # Second upload (same data, no dedup → should be appended)
        files = {"file": ("export.csv", csv_data, "text/csv")}
        resp2 = client.post(
            "/api/upload/import",
            files=files, data={"platform": "ebay"}, headers=headers,
        )
        assert resp2.status_code == 200, resp2.text
        # Final transaction count should be sum of both
        resp = client.get("/api/transactions")
        assert resp.status_code == 200
        assert len(resp.json()) == first_count + resp2.json()["created"]


# ---------------------------------------------------------------------------
# Acquisition-type combinations
# ---------------------------------------------------------------------------


class TestAcquisitionTypeMix:
    def test_mixed_acquisition_types_summarize(self, sample_txn_factory):
        # Mix purchase, gift, inheritance, donation in one summary
        txns = [
            sample_txn_factory(
                acquisition_type=AcquisitionType.PURCHASE,
                sale_price=200, purchase_price=100,
                platform_fees=0, shipping_cost_out=0,
                purchase_date=date(2023, 1, 1),
                sale_date=date(2025, 6, 1),
            ),
            sample_txn_factory(
                acquisition_type=AcquisitionType.GIFT,
                purchase_price=0, purchase_date=None,
                donor_basis=50, fmv_at_gift=80,
                gift_date=date(2024, 1, 1),
                donor_holding_period_start=date(2020, 1, 1),
                sale_date=date(2025, 6, 1),
                sale_price=150, platform_fees=0, shipping_cost_out=0,
            ),
            sample_txn_factory(
                acquisition_type=AcquisitionType.INHERITANCE,
                purchase_price=0, purchase_date=None,
                date_of_death=date(2025, 1, 1),
                fmv_at_death=500,
                sale_date=date(2025, 6, 1),
                sale_price=600, platform_fees=0, shipping_cost_out=0,
            ),
            sample_txn_factory(
                acquisition_type=AcquisitionType.DONATION,
                sale_price=0, fmv_at_donation=1000,
                purchase_price=200, purchase_date=date(2020, 1, 1),
                sale_date=date(2025, 6, 1),
                platform_fees=0, shipping_cost_out=0,
            ),
        ]
        s = summarize(txns, 80_000, FilingStatus.SINGLE, Classification.INVESTOR)
        # Three sales contribute; donation excluded
        # Purchase: +100, Gift: 150-50=100, Inheritance: 600-500=100 = +300 LT total
        assert s.net_long_term == pytest.approx(300.0)
        # Charitable deduction should be populated
        assert s.charitable_deduction_basis > 0 or s.charitable_deduction_fmv > 0
