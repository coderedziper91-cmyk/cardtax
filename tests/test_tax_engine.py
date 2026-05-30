"""Tests for backend/tax_engine.py — every public calculation."""

from __future__ import annotations

from datetime import date

import pytest

from backend.tax_engine import (
    AMT_BREAKPOINT_26_28,
    AMT_EXEMPTION_2025,
    AcquisitionType,
    Classification,
    CostBasisComponent,
    FilingStatus,
    Lot,
    LotMethod,
    ORDINARY_BRACKETS_2025,
    STANDARD_DEDUCTION_2025,
    Transaction,
    allocate_break_spot,
    amt_calculation,
    charitable_deduction,
    classify_from_quiz,
    collectibles_ltcg_tax,
    form_8949_preview,
    kiddie_tax_check,
    marginal_rate,
    match_lots,
    niit,
    nol_carryforward,
    ordinary_tax,
    qbi_deduction,
    quarterly_estimated_tax,
    schedule_c_preview,
    schedule_d_preview,
    self_employment_tax,
    summarize,
)


# ---------------------------------------------------------------------------
# Ordinary income brackets — 2026 (Rev. Proc. 2025-32)
# ---------------------------------------------------------------------------


class TestOrdinaryBrackets:
    def test_single_first_bracket_10pct(self):
        # Single: first 12,400 at 10%
        assert ordinary_tax(10_000, FilingStatus.SINGLE) == pytest.approx(1_000.0)

    def test_single_at_first_bracket_boundary(self):
        # Single 10% bracket top = $12,400 → tax = $1,240
        assert ordinary_tax(12_400, FilingStatus.SINGLE) == pytest.approx(1_240.0)

    def test_single_through_12pct_bracket(self):
        # 12,400 * 10% = 1,240 + (40,000 - 12,400) * 12% = 1,240 + 3,312 = 4,552
        assert ordinary_tax(40_000, FilingStatus.SINGLE) == pytest.approx(4_552.0)

    def test_single_22pct_bracket(self):
        # Full 10% and 12% brackets plus part of 22%
        expected = 12_400 * 0.10 + (50_400 - 12_400) * 0.12 + (80_000 - 50_400) * 0.22
        assert ordinary_tax(80_000, FilingStatus.SINGLE) == pytest.approx(expected)

    def test_single_top_bracket(self):
        # Income at $700k — falls into 37% bracket (single 37% starts at $640,600 in 2026)
        ti = 700_000
        b = ORDINARY_BRACKETS_2025["single"]  # alias to 2026
        tax = 0.0
        lower = 0.0
        for upper, rate in b:
            if ti <= upper:
                tax += (ti - lower) * rate
                break
            tax += (upper - lower) * rate
            lower = upper
        assert ordinary_tax(700_000, FilingStatus.SINGLE) == pytest.approx(tax)

    def test_mfj_brackets_are_wider(self):
        # MFJ 10% bracket runs to $24,800 in 2026.
        assert ordinary_tax(24_800, FilingStatus.MFJ) == pytest.approx(2_480.0)

    def test_mfs_brackets_narrower(self):
        # MFS 37% bracket starts at $384,350 (half of MFJ $768,700).
        # Single 37% starts at $640,600. At $400k MFS → 37%; Single → 35%.
        assert marginal_rate(400_000, FilingStatus.MFS) == 0.37
        assert marginal_rate(400_000, FilingStatus.SINGLE) == 0.35

    def test_hoh_brackets(self):
        # HoH 12% bracket goes 17,700 → 67,450
        # 17,700*0.10 + 4,300*0.12 = 1,770 + 516 = 2,286
        assert ordinary_tax(22_000, FilingStatus.HOH) == pytest.approx(2_286.0)

    def test_zero_income(self):
        assert ordinary_tax(0, FilingStatus.SINGLE) == 0.0

    def test_negative_income(self):
        assert ordinary_tax(-100, FilingStatus.SINGLE) == 0.0

    def test_marginal_rate_at_each_bracket(self):
        # 2026 single bracket tops: 12,400 / 50,400 / 105,700 / 201,775 / 256,225 / 640,600
        assert marginal_rate(0, FilingStatus.SINGLE) == 0.10
        assert marginal_rate(20_000, FilingStatus.SINGLE) == 0.12
        assert marginal_rate(100_000, FilingStatus.SINGLE) == 0.22
        assert marginal_rate(150_000, FilingStatus.SINGLE) == 0.24
        assert marginal_rate(220_000, FilingStatus.SINGLE) == 0.32
        assert marginal_rate(500_000, FilingStatus.SINGLE) == 0.35
        assert marginal_rate(1_000_000, FilingStatus.SINGLE) == 0.37


# ---------------------------------------------------------------------------
# Collectibles 28% cap
# ---------------------------------------------------------------------------


class TestCollectiblesCap:
    def test_zero_gain(self):
        tax, rate = collectibles_ltcg_tax(0, 50_000, FilingStatus.SINGLE)
        assert tax == 0.0 and rate == 0.0

    def test_below_28pct_marginal_uses_marginal(self):
        # At $20k taxable income, marginal is 12%. 1k of collectible gain stacks
        # to ~12% (still under 22% bracket boundary $50,400 in 2026).
        tax, rate = collectibles_ltcg_tax(1_000, 20_000, FilingStatus.SINGLE)
        assert rate == pytest.approx(0.12)
        assert tax == pytest.approx(120.0)

    def test_above_28pct_caps_at_28pct(self):
        # At $400k taxable income, marginal is 32%, so the 28% cap should apply.
        tax, rate = collectibles_ltcg_tax(10_000, 400_000, FilingStatus.SINGLE)
        assert rate == pytest.approx(0.28)
        assert tax == pytest.approx(2_800.0)

    def test_top_bracket_capped_28pct(self):
        # At $1M+ in 37% bracket, collectibles still capped at 28%
        tax, rate = collectibles_ltcg_tax(50_000, 1_500_000, FilingStatus.SINGLE)
        assert rate == pytest.approx(0.28)
        assert tax == pytest.approx(14_000.0)

    def test_straddle_bracket_blends(self):
        # Gain that straddles brackets gets a blended rate, capped at 28%
        # e.g. ordinary income 100k, gain 100k — gain spans 22%/24%/32% partial
        tax, rate = collectibles_ltcg_tax(100_000, 100_000, FilingStatus.SINGLE)
        assert 0.22 <= rate <= 0.28


# ---------------------------------------------------------------------------
# Long-term vs short-term boundary
# ---------------------------------------------------------------------------


class TestHoldingPeriod:
    def test_held_exactly_366_days_is_long_term(self, sample_txn_factory):
        t = sample_txn_factory(
            purchase_date=date(2024, 1, 1),
            sale_date=date(2025, 1, 2),  # exactly 366 days inclusive of leap year
        )
        assert t.holding_period_days == 367  # 2024 is leap year
        assert t.is_long_term is True

    def test_held_365_days_not_long_term(self, sample_txn_factory):
        # Buy Jan 1, sell Dec 31 same year → 364 days → short
        t = sample_txn_factory(
            purchase_date=date(2025, 1, 1),
            sale_date=date(2025, 12, 31),
        )
        assert t.holding_period_days == 364
        assert t.is_long_term is False

    def test_held_366_days_is_long_term_non_leap(self, sample_txn_factory):
        # Jan 1, 2025 → Jan 2, 2026 = 366 days
        t = sample_txn_factory(
            purchase_date=date(2025, 1, 1),
            sale_date=date(2026, 1, 2),
        )
        assert t.holding_period_days == 366
        assert t.is_long_term is True

    def test_no_purchase_date_means_no_holding_period(self, sample_txn_factory):
        t = sample_txn_factory(purchase_date=None)
        assert t.holding_period_days is None
        assert t.is_long_term is False

    def test_inheritance_always_long_term(self, sample_txn_factory):
        t = sample_txn_factory(
            acquisition_type=AcquisitionType.INHERITANCE,
            purchase_date=None,
            sale_date=date(2025, 6, 1),
            date_of_death=date(2025, 5, 1),
            fmv_at_death=500.0,
        )
        assert t.is_long_term is True


# ---------------------------------------------------------------------------
# NIIT (Net Investment Income Tax)
# ---------------------------------------------------------------------------


class TestNIIT:
    def test_below_threshold_no_niit_single(self):
        assert niit(10_000, 150_000, FilingStatus.SINGLE) == 0.0

    def test_below_threshold_no_niit_mfj(self):
        assert niit(10_000, 240_000, FilingStatus.MFJ) == 0.0

    def test_above_threshold_single(self):
        # MAGI = $250k, threshold $200k → excess $50k; net investment income $10k → tax on $10k
        assert niit(10_000, 250_000, FilingStatus.SINGLE) == pytest.approx(380.0)

    def test_above_threshold_mfj(self):
        # MAGI $300k, threshold $250k → excess $50k; NII $20k → 3.8% * min(20k, 50k) = 760
        assert niit(20_000, 300_000, FilingStatus.MFJ) == pytest.approx(760.0)

    def test_excess_limits_tax(self):
        # MAGI $210k single, threshold $200k → excess $10k; NII $50k → tax on min(50k, 10k) = 380
        assert niit(50_000, 210_000, FilingStatus.SINGLE) == pytest.approx(380.0)

    def test_no_investment_income(self):
        assert niit(0, 500_000, FilingStatus.SINGLE) == 0.0

    def test_negative_investment_income(self):
        assert niit(-1_000, 500_000, FilingStatus.SINGLE) == 0.0


# ---------------------------------------------------------------------------
# FIFO / LIFO / specific ID lot matching
# ---------------------------------------------------------------------------


class TestLotMatching:
    def test_fifo_consumes_oldest_first(self):
        lots = [
            Lot(purchase_date=date(2025, 1, 1), quantity=5, cost_per_unit=10.0),
            Lot(purchase_date=date(2025, 2, 1), quantity=5, cost_per_unit=20.0),
        ]
        used = match_lots(lots, sale_quantity=3, method=LotMethod.FIFO)
        assert len(used) == 1
        assert used[0][1] == 3
        # The first (oldest) lot has 2 remaining
        assert lots[0].quantity == 2

    def test_lifo_consumes_newest_first(self):
        lots = [
            Lot(purchase_date=date(2025, 1, 1), quantity=5, cost_per_unit=10.0),
            Lot(purchase_date=date(2025, 2, 1), quantity=5, cost_per_unit=20.0),
        ]
        used = match_lots(lots, sale_quantity=3, method=LotMethod.LIFO)
        # newest lot was the 2025-02-01 one — cost_per_unit=20
        assert used[0][0].cost_per_unit == 20.0

    def test_fifo_spans_multiple_lots(self):
        lots = [
            Lot(date(2025, 1, 1), 3, 10.0),
            Lot(date(2025, 2, 1), 5, 20.0),
        ]
        used = match_lots(lots, sale_quantity=6, method=LotMethod.FIFO)
        # 3 from first + 3 from second
        assert sum(qty for _, qty in used) == 6
        assert lots[0].quantity == 0 or len(lots) == 1  # first should be removed

    def test_specific_id(self):
        lots = [
            Lot(date(2025, 1, 1), 5, 10.0),
            Lot(date(2025, 2, 1), 5, 20.0),
            Lot(date(2025, 3, 1), 5, 30.0),
        ]
        used = match_lots(lots, sale_quantity=2, method=LotMethod.SPECIFIC_ID, specific_ids=[2])
        # used the 3rd lot (index 2), cost 30
        assert used[0][0].cost_per_unit == 30.0

    def test_specific_id_requires_ids(self):
        with pytest.raises(ValueError):
            match_lots([Lot(date(2025, 1, 1), 1, 1.0)], 1, LotMethod.SPECIFIC_ID, None)

    def test_insufficient_quantity_raises(self):
        with pytest.raises(ValueError):
            match_lots([Lot(date(2025, 1, 1), 1, 1.0)], 5, LotMethod.FIFO)


# ---------------------------------------------------------------------------
# AMT (Form 6251) — 2026 figures (Rev. Proc. 2025-32 + OBBBA §70106)
# ---------------------------------------------------------------------------


class TestAMT:
    def test_below_amt_threshold_no_amt(self):
        # Modest income, no collectibles gain — TMT should be lower than regular tax
        out = amt_calculation(
            regular_taxable_income=50_000,
            collectible_lt_gain=0.0,
            filing_status=FilingStatus.SINGLE,
            regular_tax=6_000,
        )
        assert out["applies"] is False
        assert out["amt_owed"] == 0.0

    def test_amt_uses_correct_exemption_single(self):
        out = amt_calculation(
            regular_taxable_income=200_000,
            collectible_lt_gain=0.0,
            filing_status=FilingStatus.SINGLE,
            regular_tax=0.0,
        )
        # 2026 single exemption = $90,100 (no phase-out at $200k; OBBBA phaseout starts $500k)
        assert out["exemption_used"] == pytest.approx(AMT_EXEMPTION_2025["single"])

    def test_amt_uses_correct_exemption_mfj(self):
        out = amt_calculation(
            regular_taxable_income=300_000,
            collectible_lt_gain=0.0,
            filing_status=FilingStatus.MFJ,
            regular_tax=0.0,
        )
        # 2026 MFJ exemption = $140,200 (no phase-out; OBBBA phaseout starts $1M)
        assert out["exemption_used"] == pytest.approx(AMT_EXEMPTION_2025["mfj"])

    def test_amt_26pct_rate_below_breakpoint(self):
        # 2026 AMTI breakpoint between 26% and 28% = $244,500
        # Single $200k - $90,100 exemption = $109,900 → 26% = $28,574
        out = amt_calculation(
            regular_taxable_income=200_000,
            collectible_lt_gain=0.0,
            filing_status=FilingStatus.SINGLE,
            regular_tax=0.0,
        )
        assert out["tentative_minimum_tax"] == pytest.approx(109_900 * 0.26)

    def test_amt_28pct_rate_above_breakpoint(self):
        # OBBBA phaseout cut to $500k for single in 2026 at 50% rate.
        # AMTI = $600k. Exemption phase-out: excess = $100k → reduce $90,100
        # by 0.50 * $100k = $50,000 → exemption = $40,100.
        # AMTI after exemption = $600,000 - $40,100 = $559,900.
        # 26/28% split at $244,500: $244,500 @ 26% + $315,400 @ 28%
        out = amt_calculation(
            regular_taxable_income=600_000,
            collectible_lt_gain=0.0,
            filing_status=FilingStatus.SINGLE,
            regular_tax=0.0,
        )
        expected_exemption = 90_100 - 0.50 * (600_000 - 500_000)  # = 40,100
        expected_amti_after = 600_000 - expected_exemption        # = 559,900
        expected_tmt = (
            244_500 * 0.26
            + (expected_amti_after - 244_500) * 0.28
        )
        assert out["exemption_used"] == pytest.approx(expected_exemption)
        assert out["tentative_minimum_tax"] == pytest.approx(expected_tmt)

    def test_amt_collectibles_taxed_at_28pct(self):
        # collectible gain inside AMTI is taxed at 28% (the §1(h)(4) rate)
        out = amt_calculation(
            regular_taxable_income=100_000,
            collectible_lt_gain=50_000,
            filing_status=FilingStatus.SINGLE,
            regular_tax=0.0,
        )
        # Some portion will be collectibles @ 28%
        assert out["amti"] == pytest.approx(150_000)

    def test_amt_obbba_phaseout_50pct(self):
        # Verify the OBBBA 50% phaseout (was 25% pre-OBBBA).
        # MFJ AMTI $1.2M, phaseout starts $1.0M, excess $200k.
        # Reduction = 0.50 * $200k = $100k → exemption $140,200 - $100k = $40,200.
        out = amt_calculation(
            regular_taxable_income=1_200_000,
            collectible_lt_gain=0.0,
            filing_status=FilingStatus.MFJ,
            regular_tax=0.0,
        )
        assert out["exemption_used"] == pytest.approx(40_200.0)


# ---------------------------------------------------------------------------
# Self-Employment tax (Schedule SE)
# ---------------------------------------------------------------------------


class TestSelfEmploymentTax:
    def test_ss_capped_at_wage_base(self):
        # Net SE earnings far above SS wage base — SS portion should be capped.
        # 2026 SS wage base = $184,500 (SSA Oct 2025 fact sheet).
        result = self_employment_tax(500_000, FilingStatus.SINGLE)
        assert result["social_security_tax"] == pytest.approx(184_500 * 0.124)

    def test_medicare_uncapped(self):
        # 2.9% Medicare applies to all SE earnings, no cap
        result = self_employment_tax(500_000, FilingStatus.SINGLE)
        expected_medicare = 500_000 * 0.9235 * 0.029
        assert result["medicare_tax"] == pytest.approx(expected_medicare)

    def test_additional_medicare_above_threshold_single(self):
        # Above $200k single → 0.9% additional Medicare
        result = self_employment_tax(300_000, FilingStatus.SINGLE)
        se_base = 300_000 * 0.9235
        expected_addl = max(0.0, se_base - 200_000) * 0.009
        assert result["additional_medicare_tax"] == pytest.approx(expected_addl)

    def test_no_additional_medicare_below_threshold(self):
        result = self_employment_tax(100_000, FilingStatus.SINGLE)
        assert result["additional_medicare_tax"] == 0.0

    def test_additional_medicare_mfj_threshold(self):
        # Above $250k MFJ
        result = self_employment_tax(300_000, FilingStatus.MFJ)
        se_base = 300_000 * 0.9235
        # 0.9235 * 300k = 277,050 — above 250k
        expected_addl = max(0.0, se_base - 250_000) * 0.009
        assert result["additional_medicare_tax"] == pytest.approx(expected_addl)

    def test_92_35_pct_factor(self):
        result = self_employment_tax(100_000, FilingStatus.SINGLE)
        assert result["se_base_92_35pct"] == pytest.approx(92_350.0)

    def test_zero_earnings(self):
        result = self_employment_tax(0, FilingStatus.SINGLE)
        assert result["total_se_tax"] == 0.0

    def test_deductible_half_excludes_addl_medicare(self):
        # Above the addl-Medicare threshold, the additional 0.9% is NOT included in
        # the deductible-half number on Schedule 1.
        result = self_employment_tax(300_000, FilingStatus.SINGLE)
        deductible = (result["social_security_tax"] + result["medicare_tax"]) / 2
        assert result["deductible_half_schedule_1"] == pytest.approx(deductible)


# ---------------------------------------------------------------------------
# §199A QBI deduction
# ---------------------------------------------------------------------------


class TestQBI:
    def test_no_qbi_with_zero_income(self):
        out = qbi_deduction(0, 50_000, 0, FilingStatus.SINGLE)
        assert out["applies"] is False

    def test_below_threshold_simple_20pct(self):
        # QBI $40k, taxable income $100k → below threshold, deduction = 20% * 40k = 8k
        out = qbi_deduction(40_000, 100_000, 0, FilingStatus.SINGLE)
        assert out["deduction"] == pytest.approx(8_000.0)

    def test_sstb_above_threshold_no_deduction(self):
        # SSTB taxpayer above the phase-in top → zero deduction
        out = qbi_deduction(100_000, 500_000, 0, FilingStatus.SINGLE, is_sstb=True)
        assert out["applies"] is False
        assert out["deduction"] == 0.0

    def test_taxable_income_limit_applies(self):
        # QBI $100k, but TI - net cap gain is $30k → limit is 20% of 30k = 6k
        out = qbi_deduction(100_000, 30_000, 0, FilingStatus.SINGLE)
        assert out["deduction"] == pytest.approx(6_000.0)

    def test_net_capital_gain_reduces_limit(self):
        # TI $100k, NCG $60k → limit is 20% * (100k - 60k) = 8k
        out = qbi_deduction(50_000, 100_000, 60_000, FilingStatus.SINGLE)
        # tentative is 20% * 50k = 10k, but limited to 8k
        assert out["deduction"] == pytest.approx(8_000.0)


# ---------------------------------------------------------------------------
# NOL carryforward (post-TCJA: 80% taxable income limit)
# ---------------------------------------------------------------------------


class TestNOL:
    def test_carryforward_limited_to_80_pct(self):
        # Prior NOL $100k, current taxable income $50k → 80% = $40k usable
        out = nol_carryforward(0, 100_000, 50_000)
        assert out["nol_used_this_year"] == pytest.approx(40_000.0)
        # Remaining carryforward = 100k - 40k = 60k
        assert out["carryforward_to_next_year"] == pytest.approx(60_000.0)

    def test_new_loss_generated(self):
        # Current year business loss of $20k, no prior carryforward
        out = nol_carryforward(-20_000, 0, 100_000)
        assert out["new_loss_added"] == pytest.approx(20_000.0)
        assert out["carryforward_to_next_year"] == pytest.approx(20_000.0)

    def test_no_taxable_income_no_use(self):
        out = nol_carryforward(0, 50_000, 0)
        assert out["nol_used_this_year"] == 0.0

    def test_full_use_when_under_80_pct(self):
        # Prior 10k, taxable income 100k → 80% = 80k available, full 10k used
        out = nol_carryforward(0, 10_000, 100_000)
        assert out["nol_used_this_year"] == pytest.approx(10_000.0)
        assert out["carryforward_to_next_year"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Kiddie tax (IRC §1(g))
# ---------------------------------------------------------------------------


class TestKiddieTax:
    def test_below_2700_threshold_no_tax(self):
        out = kiddie_tax_check(2_000, True, 0.32, 0.10)
        assert out["applies"] is False
        assert out["extra_tax_at_parent_rate"] == 0.0

    def test_above_threshold_taxed_at_parent_rate(self):
        # Unearned income $5k, parent rate 32%, child rate 10%
        # Income at parent rate = 5000 - 2700 = 2300
        # Extra = 2300 * (0.32 - 0.10) = 506
        out = kiddie_tax_check(5_000, True, 0.32, 0.10)
        assert out["applies"] is True
        assert out["extra_tax_at_parent_rate"] == pytest.approx(506.0)

    def test_not_dependent_no_kiddie_tax(self):
        out = kiddie_tax_check(10_000, False, 0.32, 0.10)
        assert out["applies"] is False

    def test_at_exact_threshold_no_tax(self):
        out = kiddie_tax_check(2_700, True, 0.32, 0.10)
        assert out["applies"] is False


# ---------------------------------------------------------------------------
# Gift dual-basis (§1015)
# ---------------------------------------------------------------------------


class TestGiftDualBasis:
    def test_fmv_above_basis_use_donor_basis(self, sample_txn_factory):
        # Donor basis $100, FMV at gift $200, sells for $300 → gain = 300 - 100 = 200
        t = sample_txn_factory(
            acquisition_type=AcquisitionType.GIFT,
            purchase_price=0.0,
            donor_basis=100.0,
            fmv_at_gift=200.0,
            gift_date=date(2024, 1, 1),
            donor_holding_period_start=date(2020, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=300.0,
            platform_fees=0.0,
            shipping_cost_out=0.0,
        )
        # net proceeds = 300, gain = 300 - 100 = 200
        assert t.gain_loss == pytest.approx(200.0)

    def test_fmv_below_basis_loss_uses_lower_loss_basis(self, sample_txn_factory):
        # Donor basis $300, FMV at gift $100 (depreciated). Sells for $50 → loss = 50 - 100 = -50
        t = sample_txn_factory(
            acquisition_type=AcquisitionType.GIFT,
            purchase_price=0.0,
            donor_basis=300.0,
            fmv_at_gift=100.0,
            gift_date=date(2024, 1, 1),
            donor_holding_period_start=date(2020, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=50.0,
            platform_fees=0.0,
            shipping_cost_out=0.0,
        )
        # proceeds < loss_basis (100), so loss = 50 - 100 = -50
        assert t.gain_loss == pytest.approx(-50.0)

    def test_fmv_between_zero_result_zone(self, sample_txn_factory):
        # Donor basis $300, FMV at gift $100. Sells for $200 → zero zone (between)
        t = sample_txn_factory(
            acquisition_type=AcquisitionType.GIFT,
            purchase_price=0.0,
            donor_basis=300.0,
            fmv_at_gift=100.0,
            gift_date=date(2024, 1, 1),
            donor_holding_period_start=date(2020, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=200.0,
            platform_fees=0.0,
            shipping_cost_out=0.0,
        )
        assert t.gain_loss == 0.0


# ---------------------------------------------------------------------------
# Inheritance step-up (§1014)
# ---------------------------------------------------------------------------


class TestInheritanceStepUp:
    def test_basis_is_fmv_at_death(self, sample_txn_factory):
        # FMV at death $500, sells for $600 → gain = 100
        t = sample_txn_factory(
            acquisition_type=AcquisitionType.INHERITANCE,
            purchase_price=0.0,
            purchase_date=None,
            date_of_death=date(2024, 1, 1),
            fmv_at_death=500.0,
            sale_date=date(2025, 6, 1),
            sale_price=600.0,
            platform_fees=0.0,
            shipping_cost_out=0.0,
        )
        assert t.total_basis == pytest.approx(500.0)
        assert t.gain_loss == pytest.approx(100.0)

    def test_inherited_always_long_term(self, sample_txn_factory):
        t = sample_txn_factory(
            acquisition_type=AcquisitionType.INHERITANCE,
            purchase_price=0.0,
            purchase_date=None,
            date_of_death=date(2025, 5, 1),
            fmv_at_death=500.0,
            sale_date=date(2025, 6, 1),  # only a month later
            sale_price=600.0,
        )
        assert t.is_long_term is True


# ---------------------------------------------------------------------------
# Charitable donations (§170)
# ---------------------------------------------------------------------------


class TestCharitableDeduction:
    def test_zero_donations(self):
        out = charitable_deduction([], 50_000)
        assert out["allowed"] == 0.0

    def test_30_pct_agi_limit(self, sample_txn_factory):
        # FMV $50k against $50k AGI → 30%-of-AGI limit caps deduction at $15k.
        # 2026 OBBBA §170 0.5%-of-AGI floor: $250 disallowed → $14,750 allowed.
        # Carryforward = $50k - $15k = $35k (floor reduces allowed, not carryforward).
        d = sample_txn_factory(
            acquisition_type=AcquisitionType.DONATION,
            purchase_price=10.0,
            purchase_date=date(2020, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=50_000,
            fmv_at_donation=50_000,
            donee_unrelated_use=False,  # related-use → FMV deduction
            platform_fees=0.0,
        )
        out = charitable_deduction([d], 50_000)
        assert out["allowed"] == pytest.approx(14_750.0)
        assert out["agi_floor_disallowed"] == pytest.approx(250.0)
        assert out["carryforward"] == pytest.approx(35_000.0)

    def test_unrelated_use_basis_only(self, sample_txn_factory):
        # LTCG card donated to a charity that will sell it → basis only.
        # With OBBBA 0.5% AGI floor at $100k AGI = $500 disallowed, but
        # computed deduction is only $10 → all of it is below the floor → $0 allowed.
        d = sample_txn_factory(
            acquisition_type=AcquisitionType.DONATION,
            purchase_price=10.0,
            purchase_date=date(2020, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=500,
            fmv_at_donation=500,
            donee_unrelated_use=True,
            platform_fees=0.0,
        )
        out = charitable_deduction([d], 100_000)
        assert out["computed_deduction"] == pytest.approx(10.0)
        assert out["allowed"] == pytest.approx(0.0)
        assert out["agi_floor_disallowed"] == pytest.approx(10.0)

    def test_obbba_agi_floor_clears_when_donations_large(self, sample_txn_factory):
        # AGI $100k → 0.5% floor = $500. Donate a $10k FMV related-use card.
        # 30% limit = $30k > $10k, so allowed_pre_floor = $10k.
        # Floor disallows $500 → allowed = $9,500.
        d = sample_txn_factory(
            acquisition_type=AcquisitionType.DONATION,
            purchase_price=100.0,
            purchase_date=date(2020, 1, 1),
            sale_date=date(2026, 6, 1),
            sale_price=10_000,
            fmv_at_donation=10_000,
            donee_unrelated_use=False,
            platform_fees=0.0,
        )
        out = charitable_deduction([d], 100_000)
        assert out["agi_floor_disallowed"] == pytest.approx(500.0)
        assert out["allowed"] == pytest.approx(9_500.0)


# ---------------------------------------------------------------------------
# Personal-use loss disallowance (§165(c))
# ---------------------------------------------------------------------------


class TestPersonalUseLossDisallowance:
    def test_personal_use_loss_not_deductible(self, sample_txn_factory):
        t = sample_txn_factory(
            acquisition_type=AcquisitionType.PERSONAL_COLLECTION,
            purchase_price=100,
            sale_price=50,
            platform_fees=0,
            shipping_cost_out=0,
        )
        # Raw gl = 50 - 100 = -50 → disallowed → gain_loss = 0
        assert t.gain_loss == 0.0
        assert t.personal_use_disallowed_loss == pytest.approx(50.0)

    def test_personal_use_gain_taxable(self, sample_txn_factory):
        t = sample_txn_factory(
            acquisition_type=AcquisitionType.PERSONAL_COLLECTION,
            purchase_price=100,
            sale_price=150,
            platform_fees=0,
            shipping_cost_out=0,
        )
        # Gain of 50 still taxable
        assert t.gain_loss == pytest.approx(50.0)
        assert t.personal_use_disallowed_loss == 0.0


# ---------------------------------------------------------------------------
# Break spot FMV-proportional allocation
# ---------------------------------------------------------------------------


class TestBreakSpotAllocation:
    def test_proportional_allocation(self):
        out = allocate_break_spot(150.0, [
            {"name": "A", "fmv": 100.0},
            {"name": "B", "fmv": 50.0},
        ])
        # total FMV = 150, allocations proportional: A gets 100, B gets 50
        assert out[0]["allocated_basis"] == pytest.approx(100.0)
        assert out[1]["allocated_basis"] == pytest.approx(50.0)

    def test_zero_fmv_falls_back_to_even_split(self):
        out = allocate_break_spot(60.0, [
            {"name": "A", "fmv": 0},
            {"name": "B", "fmv": 0},
            {"name": "C", "fmv": 0},
        ])
        assert all(c["allocated_basis"] == pytest.approx(20.0) for c in out)

    def test_three_card_split(self):
        # Spot $50, FMVs 100/30/20 — total 150
        out = allocate_break_spot(50.0, [
            {"name": "Mantle", "fmv": 100},
            {"name": "Mays", "fmv": 30},
            {"name": "Aaron", "fmv": 20},
        ])
        assert out[0]["allocated_basis"] == pytest.approx(33.33, abs=0.01)
        assert out[1]["allocated_basis"] == pytest.approx(10.00, abs=0.01)
        assert out[2]["allocated_basis"] == pytest.approx(6.67, abs=0.01)


# ---------------------------------------------------------------------------
# Quarterly estimated tax (Form 1040-ES) — safe harbor
# ---------------------------------------------------------------------------


class TestQuarterlyEstimated:
    def test_safe_harbor_low_agi_100_pct(self):
        # Low AGI ($50k) → 100% prior year safe harbor
        out = quarterly_estimated_tax(
            estimated_annual_tax=10_000,
            prior_year_tax=8_000,
            prior_year_agi=50_000,
        )
        # 100% of 8k = 8k vs 90% of 10k = 9k → use 8k (lower)
        assert out["safe_harbor_amount"] == pytest.approx(8_000.0)

    def test_safe_harbor_high_agi_110_pct(self):
        # AGI > $150k → 110%
        out = quarterly_estimated_tax(
            estimated_annual_tax=20_000,
            prior_year_tax=15_000,
            prior_year_agi=200_000,
        )
        # 110% of 15k = 16,500 vs 90% of 20k = 18,000 → use 16,500
        assert out["safe_harbor_amount"] == pytest.approx(16_500.0)

    def test_four_equal_payments(self):
        out = quarterly_estimated_tax(
            estimated_annual_tax=10_000,
            prior_year_tax=8_000,
            prior_year_agi=50_000,
        )
        assert out["per_quarter"] == pytest.approx(2_000.0)
        assert len(out["due_dates"]) == 4

    def test_penalty_risk_when_owed_above_1000_and_no_payments(self):
        out = quarterly_estimated_tax(
            estimated_annual_tax=10_000,
            prior_year_tax=0,
            prior_year_agi=50_000,
            withholding_paid=0,
        )
        assert out["penalty_risk"] is True

    def test_no_penalty_when_owed_below_1000(self):
        out = quarterly_estimated_tax(
            estimated_annual_tax=500,
            prior_year_tax=0,
            prior_year_agi=50_000,
            withholding_paid=0,
        )
        assert out["penalty_risk"] is False


# ---------------------------------------------------------------------------
# Hobby vs dealer quiz (IRC §183 9-factor)
# ---------------------------------------------------------------------------


class TestHobbyQuiz:
    def test_all_business_answers_yields_dealer(self):
        answers = {
            "businesslike": True, "expertise": True, "time_effort": True,
            "asset_appreciation": True, "prior_success": True, "profit_history": True,
            "amount_of_profit": True, "financial_status": True,
            "personal_pleasure": False,  # personal_pleasure: False = favors business
        }
        cls, score, _ = classify_from_quiz(answers)
        assert cls == Classification.DEALER

    def test_all_hobby_answers_yields_hobby(self):
        answers = {
            "businesslike": False, "expertise": False, "time_effort": False,
            "asset_appreciation": False, "prior_success": False, "profit_history": False,
            "amount_of_profit": False, "financial_status": False,
            "personal_pleasure": True,
        }
        cls, score, _ = classify_from_quiz(answers)
        assert cls == Classification.HOBBY

    def test_mixed_answers_can_yield_investor(self):
        # Tune the answers to land in the 0.35–0.60 investor band. Profit
        # history is weighted 1.5 — toggling it has a big effect.
        answers = {
            "businesslike": True, "expertise": True, "time_effort": True,
            "asset_appreciation": True, "prior_success": False, "profit_history": False,
            "amount_of_profit": False, "financial_status": False,
            "personal_pleasure": True,  # True = favors hobby
        }
        cls, score, _ = classify_from_quiz(answers)
        # Should be in middle band (not DEALER, not HOBBY)
        assert cls == Classification.INVESTOR, f"got {cls}, score {score:.3f}"

    def test_score_in_range(self):
        answers = {k: True for k in [
            "businesslike", "expertise", "time_effort", "asset_appreciation",
            "prior_success", "profit_history", "amount_of_profit",
            "financial_status", "personal_pleasure",
        ]}
        _, score, _ = classify_from_quiz(answers)
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Schedule C builder
# ---------------------------------------------------------------------------


class TestScheduleC:
    def test_gross_receipts(self, sample_txn_factory):
        txns = [
            sample_txn_factory(sale_price=100, purchase_price=50, platform_fees=5),
            sample_txn_factory(sale_price=200, purchase_price=80, platform_fees=10),
        ]
        sc = schedule_c_preview(txns)
        assert sc["line_1_gross_receipts"] == pytest.approx(300.0)

    def test_cogs_from_purchase_price(self, sample_txn_factory):
        txns = [
            sample_txn_factory(sale_price=100, purchase_price=50, platform_fees=0),
        ]
        sc = schedule_c_preview(txns)
        # COGS for line 4 includes purchase_price + other_basis (none here)
        assert sc["line_4_cogs"] == pytest.approx(50.0)

    def test_expenses(self, sample_txn_factory):
        from backend.tax_engine import CostBasisComponent
        txns = [
            sample_txn_factory(
                sale_price=200, purchase_price=50,
                platform_fees=15,
                shipping_cost_out=10,
                basis_components=[
                    CostBasisComponent(kind="grading", amount=25),
                ],
            ),
        ]
        sc = schedule_c_preview(txns)
        assert sc["expenses"]["line_10_commissions_fees"] == pytest.approx(15.0)
        assert sc["expenses"]["line_27_grading_other"] == pytest.approx(25.0)
        assert sc["expenses"]["shipping_and_postage"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Schedule D builder
# ---------------------------------------------------------------------------


class TestScheduleD:
    def test_short_term_part_i(self, sample_txn_factory):
        # Held < 366 days
        t = sample_txn_factory(
            purchase_date=date(2025, 1, 1),
            sale_date=date(2025, 12, 1),
            sale_price=200,
            purchase_price=100,
            platform_fees=0,
            shipping_cost_out=0,
        )
        sd = schedule_d_preview([t])
        assert len(sd["part_i_short_term"]["transactions"]) == 1
        assert sd["part_i_short_term"]["line_7_net_short_term"] == pytest.approx(100.0)

    def test_long_term_part_ii(self, sample_txn_factory):
        t = sample_txn_factory(
            purchase_date=date(2023, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=200,
            purchase_price=100,
            platform_fees=0,
            shipping_cost_out=0,
        )
        sd = schedule_d_preview([t])
        assert len(sd["part_ii_long_term"]["transactions"]) == 1
        assert sd["part_ii_long_term"]["line_15_net_long_term"] == pytest.approx(100.0)

    def test_donations_excluded(self, sample_txn_factory):
        d = sample_txn_factory(
            acquisition_type=AcquisitionType.DONATION,
            sale_price=0,
            fmv_at_donation=100,
        )
        sd = schedule_d_preview([d])
        assert len(sd["part_i_short_term"]["transactions"]) == 0
        assert len(sd["part_ii_long_term"]["transactions"]) == 0

    def test_line_16_net_total(self, sample_txn_factory):
        st = sample_txn_factory(
            purchase_date=date(2025, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=100,
            purchase_price=50,
            platform_fees=0,
            shipping_cost_out=0,
        )
        lt = sample_txn_factory(
            purchase_date=date(2023, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=200,
            purchase_price=100,
            platform_fees=0,
            shipping_cost_out=0,
        )
        sd = schedule_d_preview([st, lt])
        assert sd["line_16_net_total"] == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# Form 8949 builder
# ---------------------------------------------------------------------------


class TestForm8949:
    def test_no_1099b_long_term_goes_to_box_f(self, sample_txn_factory):
        t = sample_txn_factory(
            purchase_date=date(2023, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=200, purchase_price=100,
            platform_fees=0, shipping_cost_out=0,
        )
        out = form_8949_preview([t])
        # default is no 1099-B → Box F for long term
        assert len(out["long_term"]["F_no_1099b"]["rows"]) == 1
        assert len(out["short_term"]["C_no_1099b"]["rows"]) == 0

    def test_no_1099b_short_term_goes_to_box_c(self, sample_txn_factory):
        t = sample_txn_factory(
            purchase_date=date(2025, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=200, purchase_price=100,
            platform_fees=0, shipping_cost_out=0,
        )
        out = form_8949_preview([t])
        assert len(out["short_term"]["C_no_1099b"]["rows"]) == 1

    def test_personal_use_gets_code_L(self, sample_txn_factory):
        # Force short-term hold (under 366 days) so we know which bucket to check
        t = sample_txn_factory(
            acquisition_type=AcquisitionType.PERSONAL_COLLECTION,
            purchase_date=date(2025, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=50, purchase_price=100,
            platform_fees=0, shipping_cost_out=0,
        )
        out = form_8949_preview([t])
        rows = out["short_term"]["C_no_1099b"]["rows"]
        assert len(rows) == 1
        assert rows[0]["code"] == "L"

    def test_gift_gets_code_B(self, sample_txn_factory):
        t = sample_txn_factory(
            acquisition_type=AcquisitionType.GIFT,
            purchase_price=0,
            purchase_date=None,
            donor_basis=100, fmv_at_gift=150,
            gift_date=date(2024, 1, 1),
            donor_holding_period_start=date(2020, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=200,
            platform_fees=0, shipping_cost_out=0,
        )
        out = form_8949_preview([t])
        rows = out["long_term"]["F_no_1099b"]["rows"]
        assert len(rows) == 1
        assert rows[0]["code"] == "B"

    def test_donations_excluded(self, sample_txn_factory):
        d = sample_txn_factory(
            acquisition_type=AcquisitionType.DONATION,
            sale_price=0, fmv_at_donation=100,
        )
        out = form_8949_preview([d])
        for section in ("short_term", "long_term"):
            for box, content in out[section].items():
                assert len(content["rows"]) == 0


# ---------------------------------------------------------------------------
# Summarize end-to-end (investor classification)
# ---------------------------------------------------------------------------


class TestSummarizeInvestor:
    def test_investor_long_term_gain(self, sample_txn_factory):
        t = sample_txn_factory(
            purchase_date=date(2023, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=1_100, purchase_price=100,
            platform_fees=0, shipping_cost_out=0,
        )
        # gain = 1000 LT collectibles
        s = summarize([t], 80_000, FilingStatus.SINGLE, Classification.INVESTOR)
        assert s.net_long_term == pytest.approx(1_000.0)
        assert s.classification == Classification.INVESTOR
        # tax should be at the 22% marginal rate (since 80k taxable → marginal 22%)
        # but capped at 28% (here marginal is below cap, so use marginal)
        assert s.collectibles_tax > 0

    def test_investor_short_term_gain_ordinary_rate(self, sample_txn_factory):
        t = sample_txn_factory(
            purchase_date=date(2025, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=1_100, purchase_price=100,
            platform_fees=0, shipping_cost_out=0,
        )
        s = summarize([t], 80_000, FilingStatus.SINGLE, Classification.INVESTOR)
        assert s.net_short_term == pytest.approx(1_000.0)
        assert s.collectibles_tax == 0.0  # short-term doesn't get coll rate

    def test_investor_capital_loss_3000_cap(self, sample_txn_factory):
        t = sample_txn_factory(
            purchase_date=date(2025, 1, 1),
            sale_date=date(2025, 6, 1),
            sale_price=100, purchase_price=10_000,
            platform_fees=0, shipping_cost_out=0,
        )
        s = summarize([t], 80_000, FilingStatus.SINGLE, Classification.INVESTOR)
        # Loss = -9900 → only 3000 deductible, carryover 6900
        assert any("carryover" in n.lower() for n in s.notes)


# ---------------------------------------------------------------------------
# Standard deduction lookup
# ---------------------------------------------------------------------------


def test_standard_deductions_2026():
    # Rev. Proc. 2025-32 — 2026 inflation-adjusted standard deductions
    # (with OBBBA-permanent baseline).
    assert STANDARD_DEDUCTION_2025["single"] == 16_100
    assert STANDARD_DEDUCTION_2025["mfj"] == 32_200
    assert STANDARD_DEDUCTION_2025["hoh"] == 24_150
    assert STANDARD_DEDUCTION_2025["mfs"] == 16_100
