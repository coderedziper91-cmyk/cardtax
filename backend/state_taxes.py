"""
CardTax state tax engine.

Per-state rules for taxing collectible capital gains, OBBBA conformity status,
and the federal SALT (state and local tax) deduction cap as raised by OBBBA.

Sources:
- Tax Foundation 2025 state individual income tax rates
- State revenue department guidance (CA FTB, NY DTF, MA DOR, etc.)
- OBBBA §70401 (SALT cap increase)
- CA AB 1219 / FTB conformity statements re: IRC selective conformity

NOTE: state tax law changes frequently. This module reflects the best
publicly available information as of the 2025 tax year. Users should
verify with a CPA before filing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ConformityType(str, Enum):
    ROLLING = "rolling"       # auto-conforms to current IRC
    STATIC = "static"         # conforms as of a fixed date
    SELECTIVE = "selective"   # picks and chooses IRC sections
    NONE = "none"             # no income tax / not applicable


class StateGainTreatment(str, Enum):
    AS_ORDINARY = "as_ordinary"           # taxed at ordinary state rates
    FLAT = "flat"                          # state-wide flat rate
    SPECIAL_COLLECTIBLES = "special_coll"  # state has a special collectibles rate
    NONE = "none"                          # no income tax


@dataclass
class StateTaxProfile:
    code: str
    name: str
    has_income_tax: bool
    is_flat: bool
    flat_rate: float = 0.0
    # Progressive brackets: list of (upper_bound, marginal_rate). inf at top.
    # Single-filer brackets; we approximate MFJ by doubling thresholds when
    # the state actually doubles them (most do).
    brackets_single: list[tuple[float, float]] = field(default_factory=list)
    brackets_mfj_doubles: bool = True
    top_rate: float = 0.0
    cap_gains_treatment: StateGainTreatment = StateGainTreatment.AS_ORDINARY
    collectibles_rate_override: Optional[float] = None  # MA = 0.12
    short_term_rate_override: Optional[float] = None     # MA = 0.085
    obbba_conformity: ConformityType = ConformityType.ROLLING
    conformity_note: str = ""
    notes: str = ""


# ---------------------------------------------------------------------------
# State profiles
# ---------------------------------------------------------------------------
# Bracket data is 2025 single-filer (or post-2024 most recent). Rates rounded.

NO_TAX_STATES = ["AK", "FL", "NV", "NH", "SD", "TN", "TX", "WA", "WY"]

STATES: dict[str, StateTaxProfile] = {
    "AL": StateTaxProfile("AL", "Alabama", True, False, top_rate=0.05,
        brackets_single=[(500, 0.02), (3_000, 0.04), (float("inf"), 0.05)],
        conformity_note="Selective IRC conformity."),
    "AK": StateTaxProfile("AK", "Alaska", False, False, obbba_conformity=ConformityType.NONE,
        cap_gains_treatment=StateGainTreatment.NONE, notes="No state income tax."),
    "AZ": StateTaxProfile("AZ", "Arizona", True, True, flat_rate=0.025, top_rate=0.025,
        cap_gains_treatment=StateGainTreatment.FLAT,
        notes="Flat 2.5% as of 2023. 25% capital gains subtraction for LT assets acquired after 12/31/2011."),
    "AR": StateTaxProfile("AR", "Arkansas", True, False, top_rate=0.039,
        brackets_single=[(4_500, 0.02), (8_900, 0.039), (float("inf"), 0.039)],
        notes="50% LTCG exclusion."),
    "CA": StateTaxProfile("CA", "California", True, False, top_rate=0.133,
        brackets_single=[(10_756, 0.01), (25_499, 0.02), (40_245, 0.04), (55_866, 0.06),
                         (70_606, 0.08), (360_659, 0.093), (432_787, 0.103),
                         (721_314, 0.113), (float("inf"), 0.123)],
        obbba_conformity=ConformityType.STATIC,
        conformity_note="Conforms to IRC as of Jan 1, 2015 with selective updates. "
                        "Did NOT adopt OBBBA. Treats all capital gains as ordinary income — "
                        "no preferential rate for collectibles or LT gains.",
        notes="1% mental health surcharge over $1M = effective 14.4% top rate. CA does NOT recognize the federal 28% collectibles cap."),
    "CO": StateTaxProfile("CO", "Colorado", True, True, flat_rate=0.044, top_rate=0.044,
        cap_gains_treatment=StateGainTreatment.FLAT),
    "CT": StateTaxProfile("CT", "Connecticut", True, False, top_rate=0.0699,
        brackets_single=[(10_000, 0.02), (50_000, 0.045), (100_000, 0.055),
                         (200_000, 0.06), (250_000, 0.065), (500_000, 0.069),
                         (float("inf"), 0.0699)]),
    "DE": StateTaxProfile("DE", "Delaware", True, False, top_rate=0.066,
        brackets_single=[(2_000, 0.00), (5_000, 0.022), (10_000, 0.039),
                         (20_000, 0.048), (25_000, 0.052), (60_000, 0.0555),
                         (float("inf"), 0.066)]),
    "DC": StateTaxProfile("DC", "District of Columbia", True, False, top_rate=0.1075,
        brackets_single=[(10_000, 0.04), (40_000, 0.06), (60_000, 0.065),
                         (250_000, 0.085), (500_000, 0.0925), (1_000_000, 0.0975),
                         (float("inf"), 0.1075)]),
    "FL": StateTaxProfile("FL", "Florida", False, False, obbba_conformity=ConformityType.NONE,
        cap_gains_treatment=StateGainTreatment.NONE, notes="No state income tax."),
    "GA": StateTaxProfile("GA", "Georgia", True, True, flat_rate=0.0539, top_rate=0.0539,
        cap_gains_treatment=StateGainTreatment.FLAT,
        notes="Moving to flat 4.99% by 2029."),
    "HI": StateTaxProfile("HI", "Hawaii", True, False, top_rate=0.11,
        brackets_single=[(2_400, 0.014), (4_800, 0.032), (9_600, 0.055), (14_400, 0.064),
                         (19_200, 0.068), (24_000, 0.072), (36_000, 0.076), (48_000, 0.079),
                         (150_000, 0.0825), (175_000, 0.09), (200_000, 0.10), (float("inf"), 0.11)],
        notes="LTCG capped at 7.25% (effective rate ceiling)."),
    "ID": StateTaxProfile("ID", "Idaho", True, True, flat_rate=0.053, top_rate=0.053,
        cap_gains_treatment=StateGainTreatment.FLAT),
    "IL": StateTaxProfile("IL", "Illinois", True, True, flat_rate=0.0495, top_rate=0.0495,
        cap_gains_treatment=StateGainTreatment.FLAT),
    "IN": StateTaxProfile("IN", "Indiana", True, True, flat_rate=0.0305, top_rate=0.0305,
        cap_gains_treatment=StateGainTreatment.FLAT,
        notes="Plus county income tax (avg ~1.5%, not modeled)."),
    "IA": StateTaxProfile("IA", "Iowa", True, True, flat_rate=0.038, top_rate=0.038,
        cap_gains_treatment=StateGainTreatment.FLAT,
        notes="Moved to flat 3.8% in 2025 (formerly progressive)."),
    "KS": StateTaxProfile("KS", "Kansas", True, False, top_rate=0.057,
        brackets_single=[(23_000, 0.052), (float("inf"), 0.057)]),
    "KY": StateTaxProfile("KY", "Kentucky", True, True, flat_rate=0.04, top_rate=0.04,
        cap_gains_treatment=StateGainTreatment.FLAT),
    "LA": StateTaxProfile("LA", "Louisiana", True, True, flat_rate=0.03, top_rate=0.03,
        cap_gains_treatment=StateGainTreatment.FLAT,
        notes="Moved to flat 3% in 2025."),
    "ME": StateTaxProfile("ME", "Maine", True, False, top_rate=0.0715,
        brackets_single=[(26_800, 0.058), (63_450, 0.0675), (float("inf"), 0.0715)]),
    "MD": StateTaxProfile("MD", "Maryland", True, False, top_rate=0.0575,
        brackets_single=[(1_000, 0.02), (2_000, 0.03), (3_000, 0.04), (100_000, 0.0475),
                         (125_000, 0.05), (150_000, 0.0525), (250_000, 0.055), (float("inf"), 0.0575)],
        notes="Plus county tax 2.25%–3.20% (avg ~3%, not modeled)."),
    "MA": StateTaxProfile("MA", "Massachusetts", True, True, flat_rate=0.05, top_rate=0.05,
        cap_gains_treatment=StateGainTreatment.SPECIAL_COLLECTIBLES,
        collectibles_rate_override=0.12,
        short_term_rate_override=0.085,
        notes="MA taxes most income at flat 5%. SHORT-TERM capital gains: 8.5%. "
              "LONG-TERM gains from collectibles (and pre-1996 installment sales): 12%. "
              "Plus 4% surtax on income over $1M (effective 9% on top dollars)."),
    "MI": StateTaxProfile("MI", "Michigan", True, True, flat_rate=0.0425, top_rate=0.0425,
        cap_gains_treatment=StateGainTreatment.FLAT),
    "MN": StateTaxProfile("MN", "Minnesota", True, False, top_rate=0.0985,
        brackets_single=[(32_570, 0.0535), (106_990, 0.068), (200_000, 0.0785),
                         (float("inf"), 0.0985)],
        notes="Plus 1% NIIT-equivalent over $1M = 10.85% effective top."),
    "MS": StateTaxProfile("MS", "Mississippi", True, True, flat_rate=0.044, top_rate=0.044,
        cap_gains_treatment=StateGainTreatment.FLAT,
        notes="Phasing down toward 3% by 2030."),
    "MO": StateTaxProfile("MO", "Missouri", True, False, top_rate=0.047,
        brackets_single=[(1_273, 0.02), (2_546, 0.025), (3_819, 0.03), (5_092, 0.035),
                         (6_365, 0.04), (7_638, 0.045), (float("inf"), 0.047)]),
    "MT": StateTaxProfile("MT", "Montana", True, False, top_rate=0.059,
        brackets_single=[(20_500, 0.047), (float("inf"), 0.059)]),
    "NE": StateTaxProfile("NE", "Nebraska", True, False, top_rate=0.052,
        brackets_single=[(3_700, 0.0246), (22_170, 0.0351), (35_730, 0.0501), (float("inf"), 0.052)]),
    "NV": StateTaxProfile("NV", "Nevada", False, False, obbba_conformity=ConformityType.NONE,
        cap_gains_treatment=StateGainTreatment.NONE, notes="No state income tax."),
    "NH": StateTaxProfile("NH", "New Hampshire", False, False, obbba_conformity=ConformityType.NONE,
        cap_gains_treatment=StateGainTreatment.NONE,
        notes="No tax on wages or capital gains. Interest/dividends tax fully repealed for 2025."),
    "NJ": StateTaxProfile("NJ", "New Jersey", True, False, top_rate=0.1075,
        brackets_single=[(20_000, 0.014), (35_000, 0.0175), (40_000, 0.035), (75_000, 0.05525),
                         (500_000, 0.0637), (1_000_000, 0.0897), (float("inf"), 0.1075)],
        notes="NJ does NOT allow capital loss carryover (one-year window only)."),
    "NM": StateTaxProfile("NM", "New Mexico", True, False, top_rate=0.059,
        brackets_single=[(5_500, 0.017), (16_500, 0.032), (33_500, 0.047), (210_000, 0.049),
                         (float("inf"), 0.059)],
        notes="40% LTCG deduction (with caps)."),
    "NY": StateTaxProfile("NY", "New York", True, False, top_rate=0.109,
        brackets_single=[(8_500, 0.04), (11_700, 0.045), (13_900, 0.0525), (80_650, 0.055),
                         (215_400, 0.06), (1_077_550, 0.0685), (5_000_000, 0.0965),
                         (25_000_000, 0.103), (float("inf"), 0.109)],
        notes="NYC residents owe additional 3.078%–3.876% city income tax (not modeled)."),
    "NC": StateTaxProfile("NC", "North Carolina", True, True, flat_rate=0.0425, top_rate=0.0425,
        cap_gains_treatment=StateGainTreatment.FLAT,
        notes="Flat rate dropping to 3.99% in 2026."),
    "ND": StateTaxProfile("ND", "North Dakota", True, False, top_rate=0.025,
        brackets_single=[(47_150, 0.0), (235_750, 0.0195), (float("inf"), 0.025)]),
    "OH": StateTaxProfile("OH", "Ohio", True, False, top_rate=0.035,
        brackets_single=[(26_050, 0.0), (100_000, 0.0275), (float("inf"), 0.035)]),
    "OK": StateTaxProfile("OK", "Oklahoma", True, False, top_rate=0.0475,
        brackets_single=[(1_000, 0.0025), (2_500, 0.0075), (3_750, 0.0175), (4_900, 0.0275),
                         (7_200, 0.0375), (float("inf"), 0.0475)]),
    "OR": StateTaxProfile("OR", "Oregon", True, False, top_rate=0.099,
        brackets_single=[(4_400, 0.0475), (11_050, 0.0675), (125_000, 0.0875), (float("inf"), 0.099)],
        notes="No preferential rate for capital gains."),
    "PA": StateTaxProfile("PA", "Pennsylvania", True, True, flat_rate=0.0307, top_rate=0.0307,
        cap_gains_treatment=StateGainTreatment.FLAT,
        notes="PA does NOT allow losses to offset other income classes — net cap loss is lost."),
    "RI": StateTaxProfile("RI", "Rhode Island", True, False, top_rate=0.0599,
        brackets_single=[(77_450, 0.0375), (176_050, 0.0475), (float("inf"), 0.0599)]),
    "SC": StateTaxProfile("SC", "South Carolina", True, False, top_rate=0.062,
        brackets_single=[(3_460, 0.0), (17_330, 0.03), (float("inf"), 0.062)],
        notes="44% LTCG deduction."),
    "SD": StateTaxProfile("SD", "South Dakota", False, False, obbba_conformity=ConformityType.NONE,
        cap_gains_treatment=StateGainTreatment.NONE, notes="No state income tax."),
    "TN": StateTaxProfile("TN", "Tennessee", False, False, obbba_conformity=ConformityType.NONE,
        cap_gains_treatment=StateGainTreatment.NONE,
        notes="No income tax. Hall tax (interest/dividends) repealed in 2021."),
    "TX": StateTaxProfile("TX", "Texas", False, False, obbba_conformity=ConformityType.NONE,
        cap_gains_treatment=StateGainTreatment.NONE, notes="No state income tax."),
    "UT": StateTaxProfile("UT", "Utah", True, True, flat_rate=0.0455, top_rate=0.0455,
        cap_gains_treatment=StateGainTreatment.FLAT),
    "VT": StateTaxProfile("VT", "Vermont", True, False, top_rate=0.0875,
        brackets_single=[(45_400, 0.0335), (110_050, 0.066), (229_550, 0.076), (float("inf"), 0.0875)],
        notes="40% LTCG exclusion (with caps)."),
    "VA": StateTaxProfile("VA", "Virginia", True, False, top_rate=0.0575,
        brackets_single=[(3_000, 0.02), (5_000, 0.03), (17_000, 0.05), (float("inf"), 0.0575)]),
    "WA": StateTaxProfile("WA", "Washington", False, False, obbba_conformity=ConformityType.NONE,
        cap_gains_treatment=StateGainTreatment.NONE,
        notes="No general income tax, but a 7% LTCG tax applies on gains over ~$270k. "
              "Tangible personal property held in WA generally excluded but collectibles "
              "treatment is unsettled — consult a WA CPA."),
    "WV": StateTaxProfile("WV", "West Virginia", True, False, top_rate=0.0482,
        brackets_single=[(10_000, 0.0222), (25_000, 0.0296), (40_000, 0.0334),
                         (60_000, 0.0444), (float("inf"), 0.0482)]),
    "WI": StateTaxProfile("WI", "Wisconsin", True, False, top_rate=0.0765,
        brackets_single=[(14_320, 0.035), (28_640, 0.044), (315_310, 0.053), (float("inf"), 0.0765)],
        notes="30% LTCG exclusion."),
    "WY": StateTaxProfile("WY", "Wyoming", False, False, obbba_conformity=ConformityType.NONE,
        cap_gains_treatment=StateGainTreatment.NONE, notes="No state income tax."),
}


# ---------------------------------------------------------------------------
# SALT (OBBBA §70401)
# ---------------------------------------------------------------------------
# OBBBA raised the SALT deduction cap from $10,000 to $40,000 for tax years
# 2025–2029 (with annual 1% inflation indexing). For MFS the cap is half.
#
# Phase-out: cap reduced by 30% of MAGI in excess of $500,000 ($250k MFS),
# but never below $10,000 floor. After 2029 the cap reverts to $10,000.

SALT_CAP_BASE_2025 = 40_000
SALT_CAP_PHASEOUT_START = 500_000   # $250k MFS
SALT_CAP_PHASEOUT_RATE = 0.30
SALT_CAP_FLOOR = 10_000


def salt_cap_for(year: int, filing_status: str, magi: float) -> tuple[float, str]:
    """
    Returns (effective_salt_cap, explanation).
    Applies OBBBA cap rules for 2025–2029, then $10k after 2029.
    """
    if year >= 2030:
        cap = 5_000 if filing_status == "mfs" else 10_000
        return cap, f"Post-OBBBA: $10,000 cap returns (${cap:,} for {filing_status})."

    if year < 2025:
        cap = 5_000 if filing_status == "mfs" else 10_000
        return cap, f"Pre-OBBBA TCJA cap: ${cap:,}."

    # 2025-2029: OBBBA expanded cap with high-income phaseout
    indexed = SALT_CAP_BASE_2025 * (1.01 ** (year - 2025))
    base = indexed / 2 if filing_status == "mfs" else indexed
    phase_start = SALT_CAP_PHASEOUT_START / 2 if filing_status == "mfs" else SALT_CAP_PHASEOUT_START
    floor = SALT_CAP_FLOOR / 2 if filing_status == "mfs" else SALT_CAP_FLOOR

    excess = max(0.0, magi - phase_start)
    reduction = excess * SALT_CAP_PHASEOUT_RATE
    effective = max(floor, base - reduction)

    if magi <= phase_start:
        return effective, f"OBBBA SALT cap ${effective:,.0f} (no phase-out — MAGI below ${phase_start:,.0f})."
    return effective, (
        f"OBBBA SALT cap ${effective:,.0f}: ${base:,.0f} base reduced by 30% × "
        f"${excess:,.0f} MAGI excess (floor ${floor:,.0f})."
    )


# ---------------------------------------------------------------------------
# State tax computation
# ---------------------------------------------------------------------------


def state_tax_on_brackets(taxable_income: float, brackets: list[tuple[float, float]]) -> float:
    if taxable_income <= 0:
        return 0.0
    tax = 0.0
    lower = 0.0
    for upper, rate in brackets:
        if taxable_income <= upper:
            tax += (taxable_income - lower) * rate
            return tax
        tax += (upper - lower) * rate
        lower = upper
    return tax


def compute_state_tax(
    state_code: str,
    ordinary_income: float,
    short_term_gain: float,
    long_term_collectible_gain: float,
    filing_status: str,
) -> dict:
    """
    Compute state income tax on the user's combined income.

    Returns a dict with:
      total_state_tax, tax_on_ordinary, tax_on_st_gain, tax_on_lt_collectible_gain,
      effective_state_rate, profile, notes
    """
    profile = STATES.get(state_code.upper())
    if profile is None:
        return {
            "total_state_tax": 0.0,
            "profile": None,
            "notes": [f"Unknown state code: {state_code}"],
        }

    if not profile.has_income_tax:
        return {
            "total_state_tax": 0.0,
            "tax_on_ordinary": 0.0,
            "tax_on_st_gain": 0.0,
            "tax_on_lt_collectible_gain": 0.0,
            "effective_state_rate": 0.0,
            "profile_code": profile.code,
            "profile_name": profile.name,
            "obbba_conformity": profile.obbba_conformity.value,
            "notes": [profile.notes or f"{profile.name} has no state income tax."],
        }

    notes: list[str] = []
    if profile.conformity_note:
        notes.append(profile.conformity_note)
    if profile.notes:
        notes.append(profile.notes)

    # Resolve brackets to use
    if profile.is_flat:
        brackets = [(float("inf"), profile.flat_rate)]
    else:
        brackets = list(profile.brackets_single)
        if filing_status == "mfj" and profile.brackets_mfj_doubles:
            brackets = [(min(u * 2, float("inf")) if u != float("inf") else u, r) for u, r in brackets]

    base = max(0.0, ordinary_income)

    # Ordinary income state tax
    tax_on_ordinary = state_tax_on_brackets(base, brackets)

    # Short-term collectible gains: ordinary state rate, OR state override
    st_rate_used = None
    if profile.short_term_rate_override is not None:
        tax_on_st = short_term_gain * profile.short_term_rate_override
        st_rate_used = profile.short_term_rate_override
        notes.append(f"{profile.code}: short-term gains taxed at {profile.short_term_rate_override*100:.1f}% (state override).")
    else:
        tax_on_st = state_tax_on_brackets(base + short_term_gain, brackets) - tax_on_ordinary

    # Long-term collectible gains
    lt_rate_used = None
    if profile.collectibles_rate_override is not None:
        tax_on_lt = long_term_collectible_gain * profile.collectibles_rate_override
        lt_rate_used = profile.collectibles_rate_override
        notes.append(
            f"{profile.code} taxes LONG-TERM collectibles gains at a special "
            f"{profile.collectibles_rate_override*100:.1f}% rate (MA: G.L. c. 62 §4(d))."
        )
    else:
        # Default: long-term gains taxed as ordinary income at state level
        # (most states; states with preferential cap-gains treatment are noted in profile.notes)
        stack_base = base + short_term_gain
        tax_on_lt = (
            state_tax_on_brackets(stack_base + long_term_collectible_gain, brackets)
            - state_tax_on_brackets(stack_base, brackets)
        )

    total = tax_on_ordinary + tax_on_st + tax_on_lt
    total_income = base + short_term_gain + long_term_collectible_gain
    effective = total / total_income if total_income > 0 else 0.0

    # OBBBA divergence callout
    if profile.obbba_conformity == ConformityType.STATIC:
        notes.append(
            f"{profile.code} does not automatically conform to federal OBBBA changes. "
            "State taxable income may diverge from federal — watch for AGI add-backs."
        )

    return {
        "total_state_tax": round(total, 2),
        "tax_on_ordinary": round(tax_on_ordinary, 2),
        "tax_on_st_gain": round(tax_on_st, 2),
        "tax_on_lt_collectible_gain": round(tax_on_lt, 2),
        "st_rate_used": st_rate_used,
        "lt_rate_used": lt_rate_used,
        "effective_state_rate": effective,
        "profile_code": profile.code,
        "profile_name": profile.name,
        "obbba_conformity": profile.obbba_conformity.value,
        "notes": notes,
    }


def list_states() -> list[dict]:
    """List of all states for the UI dropdown."""
    out = []
    for code, p in sorted(STATES.items(), key=lambda kv: kv[1].name):
        out.append({
            "code": code,
            "name": p.name,
            "has_income_tax": p.has_income_tax,
            "top_rate": p.top_rate,
            "special_collectibles": p.collectibles_rate_override is not None,
        })
    return out


# ---------------------------------------------------------------------------
# Local / City income taxes
# ---------------------------------------------------------------------------
# Sources: city revenue / finance department published rates as of 2025.
# Some cities have brackets, most are flat. A few are flat fees (Denver).

from dataclasses import dataclass as _dc
from typing import Callable as _Callable


@_dc
class LocalTaxProfile:
    state_code: str
    city: str
    description: str
    # Either brackets, flat_rate, surcharge_on_state, or flat_fee_monthly
    brackets_single: list[tuple[float, float]] | None = None
    flat_rate: float | None = None              # e.g. Philadelphia 3.75%
    nonresident_rate: float | None = None       # e.g. Philadelphia 3.44%
    surcharge_on_state_tax: float | None = None # e.g. Yonkers 16.75%
    flat_fee_monthly: float | None = None       # Denver $5.75/mo
    se_surcharge_rate: float | None = None      # NYC MCTD 0.34% on SE > $50k
    se_surcharge_threshold: float | None = None
    note: str = ""


LOCAL_TAXES: dict[str, LocalTaxProfile] = {
    # New York
    "NY:NYC": LocalTaxProfile(
        "NY", "New York City",
        "NYC personal income tax — applies in addition to NY state tax (residents only).",
        brackets_single=[
            (12_000, 0.03078),
            (25_000, 0.03762),
            (50_000, 0.03819),
            (float("inf"), 0.03876),
        ],
        se_surcharge_rate=0.0034,
        se_surcharge_threshold=50_000,
        note="MCTD (Metropolitan Commuter Transportation District) self-employment surcharge of 0.34% applies on net SE earnings over $50,000.",
    ),
    "NY:Yonkers": LocalTaxProfile(
        "NY", "Yonkers",
        "16.75% surcharge ON your NY state income tax liability (residents).",
        surcharge_on_state_tax=0.1675,
        note="Non-residents pay 0.5% on Yonkers-source wages (not modeled).",
    ),

    # Pennsylvania
    "PA:Philadelphia": LocalTaxProfile(
        "PA", "Philadelphia",
        "Philadelphia Wage / Net Profits Tax — 3.75% on earnings (residents).",
        flat_rate=0.0375, nonresident_rate=0.0344,
    ),

    # Michigan
    "MI:Detroit": LocalTaxProfile(
        "MI", "Detroit",
        "Detroit city income tax — 2.4% residents.",
        flat_rate=0.024, nonresident_rate=0.012,
    ),

    # Ohio (RITA/CCA municipalities)
    "OH:Columbus":  LocalTaxProfile("OH", "Columbus",  "Columbus 2.5% flat.", flat_rate=0.025),
    "OH:Cleveland": LocalTaxProfile("OH", "Cleveland", "Cleveland 2.5% flat.", flat_rate=0.025),
    "OH:Cincinnati":LocalTaxProfile("OH", "Cincinnati","Cincinnati 1.8% flat.", flat_rate=0.018),
    "OH:Toledo":    LocalTaxProfile("OH", "Toledo",    "Toledo 2.5% flat.",    flat_rate=0.025),
    "OH:Dayton":    LocalTaxProfile("OH", "Dayton",    "Dayton 2.25% flat.",   flat_rate=0.0225),
    "OH:Akron":     LocalTaxProfile("OH", "Akron",     "Akron 2.5% flat.",     flat_rate=0.025),

    # Maryland
    "MD:Baltimore": LocalTaxProfile(
        "MD", "Baltimore",
        "Baltimore City county-level piggyback income tax — 3.2% (top of MD bracket-equivalent).",
        flat_rate=0.032,
    ),

    # Missouri
    "MO:St. Louis": LocalTaxProfile("MO", "St. Louis", "St. Louis Earnings Tax 1.0%.", flat_rate=0.010),
    "MO:Kansas City": LocalTaxProfile("MO", "Kansas City", "KC Earnings Tax 1.0%.", flat_rate=0.010),

    # Kentucky (occupational license fees)
    "KY:Louisville": LocalTaxProfile(
        "KY", "Louisville",
        "Louisville/Jefferson County occupational tax — 2.2% on net profits and wages.",
        flat_rate=0.022,
    ),

    # Alabama
    "AL:Birmingham": LocalTaxProfile("AL", "Birmingham", "Birmingham 1.0% occupational license.", flat_rate=0.010),
    "AL:Montgomery": LocalTaxProfile("AL", "Montgomery", "Montgomery 1.0% occupational tax.", flat_rate=0.010),

    # Delaware
    "DE:Wilmington": LocalTaxProfile("DE", "Wilmington", "Wilmington Wage Tax 1.25%.", flat_rate=0.0125),
    "DE:Newark": LocalTaxProfile("DE", "Newark", "Newark Wage Tax 1.5%.", flat_rate=0.015),

    # Indiana (county adjusted gross income tax — Marion County / Indianapolis)
    "IN:Indianapolis": LocalTaxProfile(
        "IN", "Indianapolis",
        "Marion County (Indianapolis) adjusted gross income tax — 2.02%.",
        flat_rate=0.0202,
    ),

    # Oregon (Metro Supportive Housing Services tax)
    "OR:Portland": LocalTaxProfile(
        "OR", "Portland (Metro SHS)",
        "Portland-area Metro 1% Supportive Housing Services tax on income over $125,000 single / $200,000 joint.",
        brackets_single=[(125_000, 0.0), (float("inf"), 0.01)],
        note="Multnomah County Preschool For All adds 1.5% above $125k / 3% above $250k (single) — not separately modeled.",
    ),

    # Colorado (flat occupational privilege)
    "CO:Denver": LocalTaxProfile(
        "CO", "Denver",
        "Denver Occupational Privilege Tax — $5.75/month flat per employed person.",
        flat_fee_monthly=5.75,
        note="Applies if you work in Denver and earn over $500/month from that work. Not a percentage of income.",
    ),
}


def list_local_for_state(state_code: str) -> list[dict]:
    """Returns localities available in the given state, for the UI dropdown."""
    out = []
    for key, p in LOCAL_TAXES.items():
        if p.state_code == state_code.upper():
            out.append({
                "key": key,
                "city": p.city,
                "description": p.description,
            })
    return out


def list_all_local() -> list[dict]:
    return [
        {"key": k, "state_code": p.state_code, "city": p.city, "description": p.description}
        for k, p in LOCAL_TAXES.items()
    ]


def compute_local_tax(
    locality_key: str,
    taxable_income: float,
    state_tax_total: float,
    se_earnings: float = 0.0,
    filing_status: str = "single",
) -> dict:
    """
    Compute local/city income tax.

    Returns dict: total_local_tax, breakdown, key, city, notes.
    """
    p = LOCAL_TAXES.get(locality_key)
    if not p:
        return {"total_local_tax": 0.0, "key": locality_key, "notes": []}

    notes: list[str] = []
    breakdown: dict[str, float] = {}

    total = 0.0
    if p.flat_rate is not None:
        amount = max(0.0, taxable_income) * p.flat_rate
        breakdown["earnings_tax"] = round(amount, 2)
        total += amount

    if p.brackets_single:
        brackets = list(p.brackets_single)
        # Naive MFJ approximation: double thresholds
        if filing_status == "mfj":
            brackets = [(min(u * 2, float("inf")) if u != float("inf") else u, r) for u, r in brackets]
        amt = state_tax_on_brackets(max(0.0, taxable_income), brackets)
        breakdown["bracket_tax"] = round(amt, 2)
        total += amt

    if p.surcharge_on_state_tax is not None:
        surcharge = state_tax_total * p.surcharge_on_state_tax
        breakdown["surcharge_on_state"] = round(surcharge, 2)
        total += surcharge
        notes.append(f"{p.city}: surcharge equals {p.surcharge_on_state_tax*100:.2f}% of NY state tax.")

    if p.flat_fee_monthly is not None:
        annual = p.flat_fee_monthly * 12
        breakdown["flat_fee_annual"] = round(annual, 2)
        total += annual
        notes.append(f"{p.city}: flat ${p.flat_fee_monthly:.2f}/month occupational privilege fee (not based on income).")

    if p.se_surcharge_rate is not None and se_earnings > (p.se_surcharge_threshold or 0):
        se_surcharge = (se_earnings - (p.se_surcharge_threshold or 0)) * p.se_surcharge_rate
        breakdown["se_surcharge_mctd"] = round(se_surcharge, 2)
        total += se_surcharge
        notes.append(f"{p.city}: MCTD self-employment surcharge {p.se_surcharge_rate*100:.2f}% applies on SE earnings over ${p.se_surcharge_threshold:,.0f}.")

    if p.note:
        notes.append(p.note)

    return {
        "total_local_tax": round(total, 2),
        "breakdown": breakdown,
        "key": locality_key,
        "state_code": p.state_code,
        "city": p.city,
        "notes": notes,
    }
