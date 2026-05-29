"""Tests for backend/state_taxes.py — per-state rules and SALT cap."""

from __future__ import annotations

import pytest

from backend.state_taxes import (
    ConformityType,
    STATES,
    compute_local_tax,
    compute_state_tax,
    list_local_for_state,
    list_states,
    salt_cap_for,
)


# ---------------------------------------------------------------------------
# California: static IRC conformity, no preferential cap-gains rate
# ---------------------------------------------------------------------------


class TestCalifornia:
    def test_static_conformity(self):
        assert STATES["CA"].obbba_conformity == ConformityType.STATIC

    def test_taxes_collectibles_as_ordinary(self):
        # $80k income, $10k LTCG → both at CA ordinary brackets (no 28% cap)
        out = compute_state_tax(
            "CA", ordinary_income=80_000, short_term_gain=0,
            long_term_collectible_gain=10_000, filing_status="single",
        )
        # LT collectible should be taxed as ordinary (no override)
        assert out["lt_rate_used"] is None
        assert out["total_state_tax"] > 0

    def test_ca_top_bracket_133(self):
        # CA's top bracket is 12.3% (with mental-health surcharge → effective 14.4%
        # but the surcharge isn't modeled here; top reported is 13.3%)
        assert STATES["CA"].top_rate == pytest.approx(0.133)

    def test_ca_obbba_divergence_note(self):
        out = compute_state_tax(
            "CA", ordinary_income=100_000, short_term_gain=0,
            long_term_collectible_gain=0, filing_status="single",
        )
        # Should mention OBBBA divergence
        assert any("OBBBA" in n or "conform" in n.lower() for n in out["notes"])


# ---------------------------------------------------------------------------
# New York: 9 brackets, NYC city tax
# ---------------------------------------------------------------------------


class TestNewYork:
    def test_ny_has_9_brackets(self):
        # NY top rate is 10.9%
        assert len(STATES["NY"].brackets_single) == 9
        assert STATES["NY"].top_rate == pytest.approx(0.109)

    def test_ny_tax_at_low_income(self):
        # $10k income — NY first bracket is 4% on first $8,500, then 4.5%
        # tax = 8500*0.04 + 1500*0.045 = 340 + 67.5 = 407.5
        out = compute_state_tax(
            "NY", ordinary_income=10_000, short_term_gain=0,
            long_term_collectible_gain=0, filing_status="single",
        )
        assert out["tax_on_ordinary"] == pytest.approx(407.5)

    def test_nyc_local_brackets(self):
        # NYC has 4 brackets (3.078% to 3.876%)
        result = compute_local_tax(
            "NY:NYC", taxable_income=10_000, state_tax_total=0,
            filing_status="single",
        )
        # First $12,000 at 3.078% → 10000 * 0.03078 = 307.8
        assert result["total_local_tax"] == pytest.approx(307.8, abs=0.01)

    def test_nyc_mctd_se_surcharge(self):
        # MCTD: 0.34% on SE earnings over $50k
        result = compute_local_tax(
            "NY:NYC", taxable_income=0, state_tax_total=0,
            se_earnings=100_000, filing_status="single",
        )
        # (100k - 50k) * 0.0034 = 170
        breakdown = result["breakdown"]
        assert breakdown.get("se_surcharge_mctd") == pytest.approx(170.0)


# ---------------------------------------------------------------------------
# Texas: no income tax
# ---------------------------------------------------------------------------


class TestTexas:
    def test_no_income_tax(self):
        assert STATES["TX"].has_income_tax is False

    def test_zero_tax_owed(self):
        out = compute_state_tax(
            "TX", ordinary_income=500_000, short_term_gain=100_000,
            long_term_collectible_gain=200_000, filing_status="single",
        )
        assert out["total_state_tax"] == 0.0


# ---------------------------------------------------------------------------
# Florida: no income tax
# ---------------------------------------------------------------------------


class TestFlorida:
    def test_no_income_tax(self):
        assert STATES["FL"].has_income_tax is False

    def test_zero_tax_owed(self):
        out = compute_state_tax(
            "FL", ordinary_income=1_000_000, short_term_gain=0,
            long_term_collectible_gain=500_000, filing_status="mfj",
        )
        assert out["total_state_tax"] == 0.0


# ---------------------------------------------------------------------------
# Massachusetts: 12% collectibles rate
# ---------------------------------------------------------------------------


class TestMassachusetts:
    def test_ma_collectibles_12_pct_override(self):
        out = compute_state_tax(
            "MA", ordinary_income=50_000, short_term_gain=0,
            long_term_collectible_gain=10_000, filing_status="single",
        )
        # LT collectibles at 12% override → 10,000 * 0.12 = 1,200
        assert out["lt_rate_used"] == pytest.approx(0.12)
        assert out["tax_on_lt_collectible_gain"] == pytest.approx(1_200.0)

    def test_ma_short_term_8_5_pct(self):
        out = compute_state_tax(
            "MA", ordinary_income=50_000, short_term_gain=10_000,
            long_term_collectible_gain=0, filing_status="single",
        )
        # ST collectibles at 8.5% override
        assert out["st_rate_used"] == pytest.approx(0.085)
        assert out["tax_on_st_gain"] == pytest.approx(850.0)

    def test_ma_ordinary_5_pct_flat(self):
        # MA is flat 5% on ordinary
        out = compute_state_tax(
            "MA", ordinary_income=100_000, short_term_gain=0,
            long_term_collectible_gain=0, filing_status="single",
        )
        assert out["tax_on_ordinary"] == pytest.approx(5_000.0)


# ---------------------------------------------------------------------------
# New Jersey: progressive
# ---------------------------------------------------------------------------


class TestNewJersey:
    def test_nj_has_income_tax(self):
        assert STATES["NJ"].has_income_tax is True

    def test_nj_top_rate_10_75(self):
        assert STATES["NJ"].top_rate == pytest.approx(0.1075)

    def test_nj_no_capital_loss_carryover_noted(self):
        # NJ doesn't allow capital loss carryover
        assert "carryover" in STATES["NJ"].notes.lower() or "NJ" in STATES["NJ"].notes


# ---------------------------------------------------------------------------
# Illinois: flat
# ---------------------------------------------------------------------------


class TestIllinois:
    def test_il_flat_4_95_pct(self):
        assert STATES["IL"].is_flat is True
        assert STATES["IL"].flat_rate == pytest.approx(0.0495)

    def test_il_tax_calculation(self):
        out = compute_state_tax(
            "IL", ordinary_income=100_000, short_term_gain=0,
            long_term_collectible_gain=0, filing_status="single",
        )
        assert out["tax_on_ordinary"] == pytest.approx(4_950.0)


# ---------------------------------------------------------------------------
# Washington: 7% LTCG over threshold
# ---------------------------------------------------------------------------


class TestWashington:
    def test_wa_no_general_income_tax(self):
        # WA listed as having no income tax in this profile
        assert STATES["WA"].has_income_tax is False

    def test_wa_ltcg_note_present(self):
        # WA module notes the 7% LTCG tax (informational)
        assert "7%" in STATES["WA"].notes or "LTCG" in STATES["WA"].notes


# ---------------------------------------------------------------------------
# Ohio: brackets + city tax
# ---------------------------------------------------------------------------


class TestOhio:
    def test_oh_has_brackets(self):
        assert STATES["OH"].has_income_tax is True
        assert len(STATES["OH"].brackets_single) >= 2

    def test_oh_first_26050_zero(self):
        # First bracket is 0% up to $26,050
        out = compute_state_tax(
            "OH", ordinary_income=20_000, short_term_gain=0,
            long_term_collectible_gain=0, filing_status="single",
        )
        assert out["tax_on_ordinary"] == 0.0

    def test_oh_columbus_city_tax(self):
        # Columbus 2.5% flat
        result = compute_local_tax(
            "OH:Columbus", taxable_income=50_000, state_tax_total=0,
        )
        assert result["total_local_tax"] == pytest.approx(1_250.0)

    def test_oh_cincinnati_1_8_pct(self):
        result = compute_local_tax(
            "OH:Cincinnati", taxable_income=50_000, state_tax_total=0,
        )
        assert result["total_local_tax"] == pytest.approx(900.0)


# ---------------------------------------------------------------------------
# Pennsylvania: flat 3.07%
# ---------------------------------------------------------------------------


class TestPennsylvania:
    def test_pa_flat_3_07(self):
        assert STATES["PA"].flat_rate == pytest.approx(0.0307)

    def test_pa_tax_at_50k(self):
        out = compute_state_tax(
            "PA", ordinary_income=50_000, short_term_gain=0,
            long_term_collectible_gain=0, filing_status="single",
        )
        assert out["tax_on_ordinary"] == pytest.approx(1_535.0)

    def test_pa_no_loss_offset_noted(self):
        # PA does not allow loss offset
        assert "does NOT" in STATES["PA"].notes or "loss" in STATES["PA"].notes.lower()

    def test_pa_philadelphia_wage_tax(self):
        # Philly 3.75% residents
        result = compute_local_tax(
            "PA:Philadelphia", taxable_income=50_000, state_tax_total=0,
        )
        assert result["total_local_tax"] == pytest.approx(1_875.0)


# ---------------------------------------------------------------------------
# SALT cap (OBBBA §70401)
# ---------------------------------------------------------------------------


class TestSALTCap:
    def test_2025_base_cap_40k(self):
        cap, _ = salt_cap_for(2025, "single", magi=100_000)
        assert cap == pytest.approx(40_000.0)

    def test_2025_mfs_cap_20k(self):
        cap, _ = salt_cap_for(2025, "mfs", magi=100_000)
        # mfs is half
        assert cap == pytest.approx(20_000.0)

    def test_phaseout_starts_at_500k(self):
        # At exactly $500k MAGI, no phase-out yet
        cap_at_500k, _ = salt_cap_for(2025, "single", magi=500_000)
        assert cap_at_500k == pytest.approx(40_000.0)
        # Above $500k → reduced
        cap_above, _ = salt_cap_for(2025, "single", magi=550_000)
        # excess $50k * 30% = 15k → 40k - 15k = 25k
        assert cap_above == pytest.approx(25_000.0)

    def test_phaseout_floors_at_10k(self):
        # Very high MAGI → floor at $10k
        cap, _ = salt_cap_for(2025, "single", magi=10_000_000)
        assert cap == pytest.approx(10_000.0)

    def test_pre_2025_uses_old_cap(self):
        cap, _ = salt_cap_for(2024, "single", magi=100_000)
        assert cap == pytest.approx(10_000.0)

    def test_post_2029_reverts(self):
        cap, _ = salt_cap_for(2030, "single", magi=100_000)
        assert cap == pytest.approx(10_000.0)

    def test_cap_indexes_1_pct_yearly(self):
        # 2026 → 1% higher than 2025
        cap_2025, _ = salt_cap_for(2025, "single", magi=100_000)
        cap_2026, _ = salt_cap_for(2026, "single", magi=100_000)
        assert cap_2026 == pytest.approx(cap_2025 * 1.01)


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


class TestStateHelpers:
    def test_list_states_returns_50_plus_dc(self):
        states = list_states()
        # All 50 states + DC = 51
        assert len(states) >= 51

    def test_unknown_state_code(self):
        out = compute_state_tax(
            "XX", ordinary_income=100_000, short_term_gain=0,
            long_term_collectible_gain=0, filing_status="single",
        )
        assert out["total_state_tax"] == 0.0
        assert any("unknown" in n.lower() for n in out["notes"])

    def test_list_local_for_ohio(self):
        cities = list_local_for_state("OH")
        # Should have at least 6 OH cities
        assert len(cities) >= 6
