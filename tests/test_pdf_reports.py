"""Tests for backend/pdf_reports.py — Form 8949, Schedule D, Schedule C, summary PDFs."""

from __future__ import annotations

from datetime import date

import pytest

from backend.pdf_reports import (
    build_form_8949_pdf,
    build_schedule_c_pdf,
    build_schedule_d_pdf,
    build_tax_summary_pdf,
)
from backend.tax_engine import (
    AcquisitionType,
    Classification,
    FilingStatus,
    form_8949_preview,
    schedule_c_preview,
    schedule_d_preview,
    self_employment_tax,
    summarize,
)


@pytest.fixture
def settings_dict():
    return {
        "tax_year": 2025,
        "filing_status": "single",
        "classification": "investor",
        "state": "CA",
        "city": "",
        "ordinary_income_estimate": 80_000,
    }


@pytest.fixture
def sample_txns(sample_txn_factory):
    """Two transactions: one short-term gain, one long-term gain."""
    return [
        sample_txn_factory(
            id=1,
            item_title="Test Card ST",
            purchase_date=date(2025, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=200, purchase_price=100,
            platform_fees=10, shipping_cost_out=0,
        ),
        sample_txn_factory(
            id=2,
            item_title="Test Card LT",
            purchase_date=date(2023, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=500, purchase_price=200,
            platform_fees=25, shipping_cost_out=5,
        ),
    ]


def _is_valid_pdf(data: bytes) -> bool:
    return isinstance(data, bytes) and len(data) > 100 and data.startswith(b"%PDF-")


# ---------------------------------------------------------------------------
# Form 8949 PDF
# ---------------------------------------------------------------------------


class TestForm8949PDF:
    def test_generates_valid_pdf_bytes(self, sample_txns, settings_dict):
        f8949 = form_8949_preview(sample_txns)
        pdf = build_form_8949_pdf(f8949, settings_dict)
        assert _is_valid_pdf(pdf)

    def test_empty_form(self, settings_dict):
        # No transactions
        f8949 = form_8949_preview([])
        pdf = build_form_8949_pdf(f8949, settings_dict)
        assert _is_valid_pdf(pdf)

    def test_pdf_has_pdf_eof_marker(self, sample_txns, settings_dict):
        f8949 = form_8949_preview(sample_txns)
        pdf = build_form_8949_pdf(f8949, settings_dict)
        # PDFs end with %%EOF
        assert b"%%EOF" in pdf[-32:] or b"%%EOF" in pdf


# ---------------------------------------------------------------------------
# Schedule D PDF
# ---------------------------------------------------------------------------


class TestScheduleDPDF:
    def test_generates_valid_pdf_bytes(self, sample_txns, settings_dict):
        sd = schedule_d_preview(sample_txns)
        # Build a real-ish summary dict
        summary = summarize(sample_txns, 80_000, FilingStatus.SINGLE, Classification.INVESTOR)
        summary_d = summary.__dict__.copy()
        summary_d["classification"] = summary.classification.value
        summary_d["filing_status"] = summary.filing_status.value
        pdf = build_schedule_d_pdf(sd, summary_d, settings_dict)
        assert _is_valid_pdf(pdf)

    def test_empty_schedule(self, settings_dict):
        sd = schedule_d_preview([])
        summary = summarize([], 50_000, FilingStatus.SINGLE, Classification.INVESTOR)
        summary_d = summary.__dict__.copy()
        summary_d["classification"] = summary.classification.value
        summary_d["filing_status"] = summary.filing_status.value
        pdf = build_schedule_d_pdf(sd, summary_d, settings_dict)
        assert _is_valid_pdf(pdf)


# ---------------------------------------------------------------------------
# Schedule C PDF (dealers only)
# ---------------------------------------------------------------------------


class TestScheduleCPDF:
    def test_generates_valid_pdf_bytes(self, sample_txns, settings_dict):
        sc = schedule_c_preview(sample_txns)
        summary = summarize(sample_txns, 80_000, FilingStatus.SINGLE, Classification.DEALER)
        summary_d = summary.__dict__.copy()
        summary_d["classification"] = summary.classification.value
        summary_d["filing_status"] = summary.filing_status.value
        se = self_employment_tax(
            summary.gross_receipts - summary.total_basis - summary.total_fees,
            FilingStatus.SINGLE,
        )
        pdf = build_schedule_c_pdf(sc, se, summary_d, settings_dict)
        assert _is_valid_pdf(pdf)

    def test_non_dealer_returns_400_via_api(self, authenticated_client, db_session):
        # The API endpoint enforces dealer classification — non-dealer → 400
        client, user = authenticated_client
        # Default classification is "investor"
        resp = client.get("/api/tax-summary/pdf/schedule-c")
        assert resp.status_code == 400

    def test_dealer_endpoint_works(self, authenticated_client, db_session):
        from backend.database import Transaction
        client, user = authenticated_client
        # Set classification to dealer
        user.settings.classification = "dealer"
        db_session.add(Transaction(
            user_id=user.id, item_title="Card",
            sale_date=date(2025, 6, 1), sale_price=100.0,
            purchase_price=40.0, platform_fees=5.0,
        ))
        db_session.commit()
        resp = client.get("/api/tax-summary/pdf/schedule-c")
        assert resp.status_code == 200
        assert _is_valid_pdf(resp.content)


# ---------------------------------------------------------------------------
# Tax summary PDF
# ---------------------------------------------------------------------------


class TestTaxSummaryPDF:
    def test_generates_valid_pdf_bytes(self, sample_txns, settings_dict):
        summary = summarize(sample_txns, 80_000, FilingStatus.SINGLE, Classification.INVESTOR)
        summary_d = summary.__dict__.copy()
        summary_d["classification"] = summary.classification.value
        summary_d["filing_status"] = summary.filing_status.value
        pdf = build_tax_summary_pdf(summary_d, settings_dict)
        assert _is_valid_pdf(pdf)


# ---------------------------------------------------------------------------
# API endpoints — end-to-end through the FastAPI app
# ---------------------------------------------------------------------------


class TestPDFEndpoints:
    def _seed_txn(self, db, user):
        from backend.database import Transaction
        db.add(Transaction(
            user_id=user.id, item_title="LT Card",
            sale_date=date(2025, 6, 1),
            sale_price=500.0, purchase_price=200.0,
            purchase_date=date(2023, 1, 1),
            platform_fees=25.0,
        ))
        db.commit()

    def test_form_8949_pdf_endpoint(self, authenticated_client, db_session):
        client, user = authenticated_client
        self._seed_txn(db_session, user)
        resp = client.get("/api/tax-summary/pdf/form-8949")
        assert resp.status_code == 200
        assert _is_valid_pdf(resp.content)

    def test_schedule_d_pdf_endpoint(self, authenticated_client, db_session):
        client, user = authenticated_client
        self._seed_txn(db_session, user)
        resp = client.get("/api/tax-summary/pdf/schedule-d")
        assert resp.status_code == 200
        assert _is_valid_pdf(resp.content)

    def test_summary_pdf_endpoint(self, authenticated_client, db_session):
        client, user = authenticated_client
        self._seed_txn(db_session, user)
        resp = client.get("/api/tax-summary/pdf/summary")
        assert resp.status_code == 200
        assert _is_valid_pdf(resp.content)
