"""
CardTax tax engine.

Implements the 2025 OBBBA (One Big Beautiful Bill Act) rules for collectibles.

Key statutes / authorities referenced:
- IRC §1(h)(4)-(5): collectibles gain taxed at max 28%
- IRC §408(m)(2): definition of "collectible"
- IRC §1411: Net Investment Income Tax (3.8%)
- IRC §183: hobby loss / 9-factor test
- IRC §1091: wash sale (does NOT apply to collectibles)
- IRC §6050W / 1099-K thresholds (OBBBA restored 2025 threshold)

All bracket figures below are the 2025 inflation-adjusted figures published by
the IRS (Rev. Proc. 2024-40) and confirmed by the OBBBA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# 2025 ordinary income tax brackets (post-OBBBA)
# ---------------------------------------------------------------------------

ORDINARY_BRACKETS_2025 = {
    "single": [
        (11_925, 0.10),
        (48_475, 0.12),
        (103_350, 0.22),
        (197_300, 0.24),
        (250_525, 0.32),
        (626_350, 0.35),
        (float("inf"), 0.37),
    ],
    "mfj": [
        (23_850, 0.10),
        (96_950, 0.12),
        (206_700, 0.22),
        (394_600, 0.24),
        (501_050, 0.32),
        (751_600, 0.35),
        (float("inf"), 0.37),
    ],
    "mfs": [
        (11_925, 0.10),
        (48_475, 0.12),
        (103_350, 0.22),
        (197_300, 0.24),
        (250_525, 0.32),
        (375_800, 0.35),
        (float("inf"), 0.37),
    ],
    "hoh": [
        (17_000, 0.10),
        (64_850, 0.12),
        (103_350, 0.22),
        (197_300, 0.24),
        (250_500, 0.32),
        (626_350, 0.35),
        (float("inf"), 0.37),
    ],
}

# 2025 standard deduction (OBBBA increased these)
STANDARD_DEDUCTION_2025 = {
    "single": 15_000,
    "mfj": 30_000,
    "mfs": 15_000,
    "hoh": 22_500,
}

# Net Investment Income Tax thresholds (IRC §1411 — not indexed for inflation)
NIIT_THRESHOLDS = {
    "single": 200_000,
    "mfj": 250_000,
    "mfs": 125_000,
    "hoh": 200_000,
}
NIIT_RATE = 0.038

# Collectibles maximum long-term rate (IRC §1(h)(4))
COLLECTIBLES_MAX_LTCG_RATE = 0.28

# 1099-K reporting thresholds (post-OBBBA restoration)
FORM_1099K_THRESHOLDS = {
    2024: {"amount": 5_000, "transactions": 0, "note": "ARP transition year"},
    2025: {"amount": 20_000, "transactions": 200, "note": "OBBBA restored pre-ARP threshold"},
    2026: {"amount": 600, "transactions": 0, "note": "OBBBA sunset — $600 threshold returns"},
}

LONG_TERM_HOLDING_DAYS = 366  # >1 year, per IRC §1222


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FilingStatus(str, Enum):
    SINGLE = "single"
    MFJ = "mfj"
    MFS = "mfs"
    HOH = "hoh"


class Classification(str, Enum):
    HOBBY = "hobby"        # Schedule 1 income, no deductions beyond income
    INVESTOR = "investor"  # Schedule D capital gains
    DEALER = "dealer"      # Schedule C ordinary income / self-employment


class LotMethod(str, Enum):
    FIFO = "fifo"
    LIFO = "lifo"
    SPECIFIC_ID = "specific_id"


class AcquisitionType(str, Enum):
    PURCHASE = "purchase"
    GIFT = "gift"                           # IRC §1015 — carryover basis with FMV-loss rule
    INHERITANCE = "inheritance"             # IRC §1014 — step-up to FMV at DoD
    BREAK_SPOT = "break_spot"               # cost basis allocated from spot price
    PERSONAL_COLLECTION = "personal_collection"  # IRC §165(c) — losses NOT deductible
    DONATION = "donation"                   # not a sale; charitable contribution
    OTHER = "other"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CostBasisComponent:
    """One line of cost basis (purchase, grading, shipping, etc.)."""
    kind: str        # 'purchase', 'grading', 'shipping_in', 'shipping_grade', 'insurance', 'break_spot', 'packaging', 'buyer_premium', 'other'
    amount: float


@dataclass
class Transaction:
    """A single sale event."""
    id: int | None
    item_title: str
    sale_date: date
    sale_price: float          # gross 1099-K amount
    platform_fees: float       # eBay/Whatnot/COMC seller fees
    shipping_charged: float = 0.0   # shipping the buyer paid (counts in sale_price)
    shipping_cost_out: float = 0.0  # what the seller actually paid to ship
    purchase_date: date | None = None
    purchase_price: float = 0.0
    basis_components: list[CostBasisComponent] = field(default_factory=list)
    platform_source: str = "unknown"
    notes: str = ""

    # Acquisition method and related per-method fields
    acquisition_type: AcquisitionType = AcquisitionType.PURCHASE

    # Gift fields (IRC §1015 dual basis)
    donor_basis: float | None = None
    gift_date: date | None = None
    fmv_at_gift: float | None = None
    donor_holding_period_start: date | None = None  # tacks on for gifts

    # Inheritance fields (IRC §1014)
    date_of_death: date | None = None
    fmv_at_death: float | None = None

    # Donation fields (not taxable; for charitable deduction calc)
    donation_date: date | None = None
    fmv_at_donation: float | None = None
    donee_organization: str = ""
    donee_unrelated_use: bool = True  # default: charity will sell → basis-only

    # Break allocation
    break_spot_price: float | None = None
    break_total_fmv: float | None = None

    @property
    def is_donation(self) -> bool:
        return self.acquisition_type == AcquisitionType.DONATION

    @property
    def is_personal_use(self) -> bool:
        return self.acquisition_type == AcquisitionType.PERSONAL_COLLECTION

    @property
    def total_basis(self) -> float:
        """Adjusted basis used for GAIN calculation (carryover basis under §1015)."""
        if self.acquisition_type == AcquisitionType.INHERITANCE:
            # IRC §1014: basis = FMV at date of death (no other costs added pre-inheritance)
            base = self.fmv_at_death or 0.0
            # Post-inheritance improvements (grading after inheritance) still count
            return base + sum(c.amount for c in self.basis_components)

        if self.acquisition_type == AcquisitionType.GIFT:
            # Carryover basis (donor's adjusted basis), plus any improvements
            # made by donee. We treat purchase_price as 0 — donor's basis is separate.
            base = self.donor_basis or 0.0
            return base + sum(c.amount for c in self.basis_components)

        return self.purchase_price + sum(c.amount for c in self.basis_components)

    @property
    def loss_basis(self) -> float:
        """
        For gift property only: §1015 loss basis = lesser of donor's basis or FMV at gift.
        For everything else, equals total_basis.
        """
        if self.acquisition_type != AcquisitionType.GIFT:
            return self.total_basis
        donor = (self.donor_basis or 0.0) + sum(c.amount for c in self.basis_components)
        fmv = (self.fmv_at_gift or 0.0) + sum(c.amount for c in self.basis_components)
        return min(donor, fmv) if (self.fmv_at_gift is not None) else donor

    @property
    def net_proceeds(self) -> float:
        return self.sale_price - self.platform_fees - self.shipping_cost_out

    @property
    def gain_loss(self) -> float:
        """
        Realized gain or loss with the dual-basis rule for gifts and the
        personal-use loss disallowance under IRC §165(c).
        """
        if self.is_donation:
            # A donation is not a sale; no realized gain/loss
            return 0.0

        proceeds = self.net_proceeds

        if self.acquisition_type == AcquisitionType.GIFT and self.fmv_at_gift is not None:
            # §1015 dual basis:
            gain_basis = self.total_basis              # donor's basis
            loss_basis = self.loss_basis               # lesser of donor's basis / FMV
            if proceeds > gain_basis:
                return proceeds - gain_basis           # gain
            if proceeds < loss_basis:
                return proceeds - loss_basis           # loss
            return 0.0                                 # zero-result zone

        gl = proceeds - self.total_basis

        # IRC §165(c): no deduction for personal-use losses (collectibles are
        # capital assets for most people, but personal-use property's losses
        # aren't deductible). Gains stay taxable.
        if self.is_personal_use and gl < 0:
            return 0.0

        return gl

    @property
    def personal_use_disallowed_loss(self) -> float:
        """The amount of loss that was DISALLOWED due to §165(c) personal-use rule."""
        if not self.is_personal_use:
            return 0.0
        raw = self.net_proceeds - self.total_basis
        return -raw if raw < 0 else 0.0

    @property
    def holding_period_days(self) -> int | None:
        # Inherited property is automatically long-term regardless of how long held
        if self.acquisition_type == AcquisitionType.INHERITANCE:
            return LONG_TERM_HOLDING_DAYS

        # Gifts: holding period tacks on from donor (if we have donor purchase date)
        start = self.purchase_date
        if self.acquisition_type == AcquisitionType.GIFT:
            start = self.donor_holding_period_start or self.gift_date or self.purchase_date

        if start is None:
            return None
        return (self.sale_date - start).days

    @property
    def is_long_term(self) -> bool:
        if self.acquisition_type == AcquisitionType.INHERITANCE:
            return True
        hp = self.holding_period_days
        return hp is not None and hp >= LONG_TERM_HOLDING_DAYS


@dataclass
class TaxSummary:
    short_term_gain: float
    short_term_loss: float
    long_term_gain: float
    long_term_loss: float
    net_short_term: float
    net_long_term: float
    total_net: float
    gross_receipts: float
    total_basis: float
    total_fees: float
    collectibles_tax: float
    niit: float
    estimated_total_tax: float
    effective_collectibles_rate: float
    classification: Classification
    filing_status: FilingStatus
    bracket_marginal_rate: float
    # Federal tax broken out
    federal_income_tax: float = 0.0
    # State tax fields (populated by the API layer)
    state_tax: float = 0.0
    state_code: str = ""
    # Charitable deduction (from DONATION transactions)
    charitable_deduction_fmv: float = 0.0
    charitable_deduction_basis: float = 0.0
    charitable_deduction_allowed: float = 0.0
    charitable_carryforward: float = 0.0
    # Personal-use loss disallowance (IRC §165(c))
    disallowed_personal_use_losses: float = 0.0
    # SALT
    salt_cap: float = 0.0
    salt_note: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Bracket math
# ---------------------------------------------------------------------------


def ordinary_tax(taxable_income: float, filing_status: FilingStatus) -> float:
    """Compute federal income tax on taxable income using 2025 brackets."""
    if taxable_income <= 0:
        return 0.0
    brackets = ORDINARY_BRACKETS_2025[filing_status.value]
    tax = 0.0
    lower = 0.0
    for upper, rate in brackets:
        if taxable_income <= upper:
            tax += (taxable_income - lower) * rate
            return tax
        tax += (upper - lower) * rate
        lower = upper
    return tax


def marginal_rate(taxable_income: float, filing_status: FilingStatus) -> float:
    """Marginal bracket rate at a given taxable-income point."""
    if taxable_income < 0:
        taxable_income = 0
    for upper, rate in ORDINARY_BRACKETS_2025[filing_status.value]:
        if taxable_income <= upper:
            return rate
    return 0.37


def collectibles_ltcg_tax(
    collectible_gain: float,
    ordinary_taxable_income: float,
    filing_status: FilingStatus,
) -> tuple[float, float]:
    """
    Tax on long-term collectibles gain.

    Per IRC §1(h)(4)-(5), collectibles gain is taxed at the LOWER of:
      (a) 28%, or
      (b) the rate that would apply if treated as ordinary income.

    Returns (tax_amount, effective_rate).
    Bracket stacking: ordinary income fills brackets first, then the
    collectibles gain stacks on top.
    """
    if collectible_gain <= 0:
        return 0.0, 0.0

    base = max(ordinary_taxable_income, 0)
    tax_if_ordinary = (
        ordinary_tax(base + collectible_gain, filing_status)
        - ordinary_tax(base, filing_status)
    )
    blended_ordinary_rate = tax_if_ordinary / collectible_gain
    effective_rate = min(COLLECTIBLES_MAX_LTCG_RATE, blended_ordinary_rate)
    return collectible_gain * effective_rate, effective_rate


def niit(
    investment_income: float,
    magi: float,
    filing_status: FilingStatus,
) -> float:
    """
    Net Investment Income Tax (IRC §1411).
    3.8% on the LESSER of net investment income or MAGI over threshold.
    """
    if investment_income <= 0:
        return 0.0
    threshold = NIIT_THRESHOLDS[filing_status.value]
    excess = max(0.0, magi - threshold)
    return NIIT_RATE * min(investment_income, excess)


# ---------------------------------------------------------------------------
# Lot matching (FIFO / LIFO / specific ID)
# ---------------------------------------------------------------------------


@dataclass
class Lot:
    purchase_date: date
    quantity: int
    cost_per_unit: float


def match_lots(
    lots: list[Lot],
    sale_quantity: int,
    method: LotMethod,
    specific_ids: list[int] | None = None,
) -> list[tuple[Lot, int]]:
    """
    Return [(lot, qty_used)] consumed by a sale, mutating `lots` in place.
    """
    consumed: list[tuple[Lot, int]] = []
    remaining = sale_quantity

    if method == LotMethod.SPECIFIC_ID:
        if not specific_ids:
            raise ValueError("specific_id method requires specific_ids")
        order = [lots[i] for i in specific_ids]
    elif method == LotMethod.LIFO:
        order = sorted(lots, key=lambda l: l.purchase_date, reverse=True)
    else:  # FIFO default
        order = sorted(lots, key=lambda l: l.purchase_date)

    for lot in order:
        if remaining <= 0:
            break
        take = min(lot.quantity, remaining)
        lot.quantity -= take
        remaining -= take
        consumed.append((lot, take))

    lots[:] = [l for l in lots if l.quantity > 0]
    if remaining > 0:
        raise ValueError(f"Not enough lots to cover sale of {sale_quantity}")
    return consumed


# ---------------------------------------------------------------------------
# Summary computation
# ---------------------------------------------------------------------------


def summarize(
    transactions: Sequence[Transaction],
    ordinary_income: float,
    filing_status: FilingStatus,
    classification: Classification,
) -> TaxSummary:
    """Compute a full TaxSummary across all transactions for the year."""

    # Donations are not sales — exclude them from gain/loss aggregation
    sales = [t for t in transactions if not t.is_donation]
    donations = [t for t in transactions if t.is_donation]

    short_gain = sum(t.gain_loss for t in sales if not t.is_long_term and t.gain_loss > 0)
    short_loss = sum(-t.gain_loss for t in sales if not t.is_long_term and t.gain_loss < 0)
    long_gain = sum(t.gain_loss for t in sales if t.is_long_term and t.gain_loss > 0)
    long_loss = sum(-t.gain_loss for t in sales if t.is_long_term and t.gain_loss < 0)

    net_short = short_gain - short_loss
    net_long = long_gain - long_loss
    total_net = net_short + net_long

    gross_receipts = sum(t.sale_price for t in sales)
    total_basis = sum(t.total_basis for t in sales)
    total_fees = sum(t.platform_fees + t.shipping_cost_out for t in sales)
    disallowed_pu = sum(t.personal_use_disallowed_loss for t in sales)

    notes: list[str] = []

    # Use standard deduction as a baseline for taxable-income estimate
    std_ded = STANDARD_DEDUCTION_2025[filing_status.value]
    base_taxable = max(0.0, ordinary_income - std_ded)
    base_marginal = marginal_rate(base_taxable, filing_status)

    collectibles_tax_amount = 0.0
    effective_coll_rate = 0.0
    extra_ordinary_tax = 0.0

    if classification == Classification.INVESTOR:
        # Long-term collectible gains: capped at 28% rate
        if net_long > 0:
            collectibles_tax_amount, effective_coll_rate = collectibles_ltcg_tax(
                net_long, base_taxable, filing_status
            )
        # Short-term gains: ordinary income (stacks on top of base income)
        if net_short > 0:
            extra_ordinary_tax = (
                ordinary_tax(base_taxable + net_short, filing_status)
                - ordinary_tax(base_taxable, filing_status)
            )
        # Capital loss: up to $3,000 ($1,500 MFS) net loss against ordinary
        if total_net < 0:
            cap_loss_limit = 1_500 if filing_status == FilingStatus.MFS else 3_000
            deductible = min(-total_net, cap_loss_limit)
            extra_ordinary_tax = -deductible * base_marginal
            carryover = -total_net - deductible
            if carryover > 0:
                notes.append(
                    f"${carryover:,.2f} capital loss carryover to next tax year (Schedule D line 21)."
                )

    elif classification == Classification.DEALER:
        # Dealer = inventory treatment, gross profit is ordinary income on Sch C.
        # Subject to self-employment tax (15.3% on first $168,600 / 2.9% above).
        dealer_profit = gross_receipts - total_basis - total_fees
        if dealer_profit > 0:
            extra_ordinary_tax = (
                ordinary_tax(base_taxable + dealer_profit, filing_status)
                - ordinary_tax(base_taxable, filing_status)
            )
            # Self-employment tax: 15.3% on 92.35% of net SE earnings up to SS cap
            se_base = dealer_profit * 0.9235
            ss_cap_2025 = 168_600
            ss_tax = min(se_base, ss_cap_2025) * 0.124
            medicare_tax = se_base * 0.029
            se_tax = ss_tax + medicare_tax
            extra_ordinary_tax += se_tax
            notes.append(
                f"Self-employment tax estimated at ${se_tax:,.2f} "
                f"(12.4% SS up to ${ss_cap_2025:,}, 2.9% Medicare uncapped)."
            )

    elif classification == Classification.HOBBY:
        # Hobby income: reported on Schedule 1, no deduction of expenses
        # (TCJA suspended miscellaneous itemized deductions through 2025; OBBBA extended).
        hobby_income = max(0.0, gross_receipts)  # gross, not net!
        extra_ordinary_tax = (
            ordinary_tax(base_taxable + hobby_income, filing_status)
            - ordinary_tax(base_taxable, filing_status)
        )
        notes.append(
            "Hobby income is reported gross on Schedule 1; expenses are NOT deductible "
            "(TCJA §67(g), extended by OBBBA). This is usually the worst tax outcome."
        )

    # NIIT — only applies to investor classification (passive investment income)
    niit_amount = 0.0
    if classification == Classification.INVESTOR:
        net_investment_income = max(0.0, total_net)
        magi_estimate = ordinary_income + max(0.0, total_net)
        niit_amount = niit(net_investment_income, magi_estimate, filing_status)
        if niit_amount > 0:
            notes.append(
                f"NIIT (IRC §1411): 3.8% applies because MAGI exceeds "
                f"${NIIT_THRESHOLDS[filing_status.value]:,}."
            )

    # 1099-K threshold notes
    notes.append(
        f"2025 1099-K threshold: $20,000 AND 200 transactions per platform. "
        f"In 2026 the threshold drops to $600 — every active seller will receive a 1099-K."
    )

    # Wash sale exception (advantage for collectibles)
    if any(t.gain_loss < 0 for t in sales) and classification == Classification.INVESTOR:
        notes.append(
            "Wash sale rules (IRC §1091) do NOT apply to collectibles — you can "
            "harvest losses and immediately repurchase the same card."
        )

    if disallowed_pu > 0:
        notes.append(
            f"${disallowed_pu:,.2f} of losses from personal-use property are NOT "
            f"deductible (IRC §165(c)). Gains on personal-use property are still taxable."
        )

    # Charitable deduction from donations
    char = charitable_deduction(donations, ordinary_income)
    if donations:
        notes.append(
            f"{len(donations)} card(s) donated to charity — see charitable deduction section."
        )

    # 1031 misconception note (only if user has non-purchase transactions)
    if any(t.acquisition_type != AcquisitionType.PURCHASE for t in sales):
        notes.append(
            "TCJA §1031 reform: Like-kind exchanges are LIMITED TO REAL PROPERTY only. "
            "Cards, comics, and other collectibles CANNOT use §1031 to defer gain. "
            "Any swap of cards is a fully taxable disposition."
        )

    return TaxSummary(
        short_term_gain=short_gain,
        short_term_loss=short_loss,
        long_term_gain=long_gain,
        long_term_loss=long_loss,
        net_short_term=net_short,
        net_long_term=net_long,
        total_net=total_net,
        gross_receipts=gross_receipts,
        total_basis=total_basis,
        total_fees=total_fees,
        collectibles_tax=collectibles_tax_amount,
        niit=niit_amount,
        federal_income_tax=collectibles_tax_amount + extra_ordinary_tax,
        estimated_total_tax=collectibles_tax_amount + extra_ordinary_tax + niit_amount,
        effective_collectibles_rate=effective_coll_rate,
        classification=classification,
        filing_status=filing_status,
        bracket_marginal_rate=base_marginal,
        charitable_deduction_fmv=char["fmv_total"],
        charitable_deduction_basis=char["basis_total"],
        charitable_deduction_allowed=char["allowed"],
        charitable_carryforward=char["carryforward"],
        disallowed_personal_use_losses=round(disallowed_pu, 2),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Schedule C / Schedule D preview builders
# ---------------------------------------------------------------------------


def schedule_c_preview(transactions: Sequence[Transaction]) -> dict:
    """
    Build a Schedule C (Profit or Loss from Business) preview.
    Line numbers reference the 2025 form.
    """
    gross_receipts = sum(t.sale_price for t in transactions)

    cogs = 0.0
    shipping_exp = 0.0
    grading_exp = 0.0
    platform_fees_total = 0.0
    other_basis = 0.0

    for t in transactions:
        cogs += t.purchase_price
        for c in t.basis_components:
            if c.kind in ("grading",):
                grading_exp += c.amount
            elif c.kind in ("shipping_in", "shipping_grade"):
                shipping_exp += c.amount
            elif c.kind == "buyer_premium":
                other_basis += c.amount
            else:
                other_basis += c.amount
        platform_fees_total += t.platform_fees
        shipping_exp += t.shipping_cost_out

    gross_profit = gross_receipts - cogs - other_basis
    total_expenses = shipping_exp + grading_exp + platform_fees_total
    net_profit = gross_profit - total_expenses

    return {
        "line_1_gross_receipts": round(gross_receipts, 2),
        "line_2_returns_allowances": 0.0,
        "line_3_net_receipts": round(gross_receipts, 2),
        "line_4_cogs": round(cogs + other_basis, 2),
        "line_5_gross_profit": round(gross_profit, 2),
        "line_7_gross_income": round(gross_profit, 2),
        "expenses": {
            "line_10_commissions_fees": round(platform_fees_total, 2),
            "line_22_supplies": 0.0,
            "line_24a_travel": 0.0,
            "line_27_grading_other": round(grading_exp, 2),
            "shipping_and_postage": round(shipping_exp, 2),
        },
        "line_28_total_expenses": round(total_expenses, 2),
        "line_31_net_profit": round(net_profit, 2),
    }


def schedule_d_preview(transactions: Sequence[Transaction]) -> dict:
    """
    Build a Schedule D / Form 8949 preview for collectible capital gains.
    Excludes donations (no sale event).
    """
    short_term: list[dict] = []
    long_term: list[dict] = []

    for t in transactions:
        if t.is_donation:
            continue
        # Date acquired display: use the controlling acquisition date for the row
        if t.acquisition_type == AcquisitionType.INHERITANCE and t.date_of_death:
            date_acq = t.date_of_death.isoformat() + " (INHERITED)"
        elif t.acquisition_type == AcquisitionType.GIFT and t.gift_date:
            date_acq = t.gift_date.isoformat() + " (GIFT — §1015)"
        else:
            date_acq = t.purchase_date.isoformat() if t.purchase_date else ""

        row = {
            "description": t.item_title,
            "date_acquired": date_acq,
            "date_sold": t.sale_date.isoformat(),
            "proceeds": round(t.net_proceeds, 2),
            "basis": round(t.total_basis, 2),
            "gain_loss": round(t.gain_loss, 2),
        }
        if t.is_long_term:
            long_term.append(row)
        else:
            short_term.append(row)

    st_total = sum(r["gain_loss"] for r in short_term)
    lt_total = sum(r["gain_loss"] for r in long_term)

    return {
        "part_i_short_term": {
            "transactions": short_term,
            "line_7_net_short_term": round(st_total, 2),
        },
        "part_ii_long_term": {
            "transactions": long_term,
            "line_15_net_long_term": round(lt_total, 2),
            "note": "28% rate gain (collectibles) — completes 28% Rate Gain Worksheet, line 18 of Schedule D",
        },
        "line_16_net_total": round(st_total + lt_total, 2),
    }


# ---------------------------------------------------------------------------
# IRC §183 9-factor hobby vs business test
# ---------------------------------------------------------------------------


HOBBY_QUIZ_FACTORS = [
    {
        "id": "businesslike",
        "question": "Do you keep complete and accurate books and records (inventory, ledger, business bank account)?",
        "weight": 1.2,
        "favors_business_if_yes": True,
    },
    {
        "id": "expertise",
        "question": "Do you have substantial knowledge or consult with experts to operate this as a business?",
        "weight": 1.0,
        "favors_business_if_yes": True,
    },
    {
        "id": "time_effort",
        "question": "Do you devote significant time and effort to the activity (vs. casual weekend selling)?",
        "weight": 1.0,
        "favors_business_if_yes": True,
    },
    {
        "id": "asset_appreciation",
        "question": "Do you expect the assets to appreciate in value (as a primary motivation)?",
        "weight": 0.7,
        "favors_business_if_yes": True,
    },
    {
        "id": "prior_success",
        "question": "Have you previously turned a similar activity from unprofitable to profitable?",
        "weight": 0.7,
        "favors_business_if_yes": True,
    },
    {
        "id": "profit_history",
        "question": "Have you had profits in at least 3 of the last 5 tax years from this activity? (IRC §183(d) safe harbor)",
        "weight": 1.5,
        "favors_business_if_yes": True,
    },
    {
        "id": "amount_of_profit",
        "question": "When you have profits, are they substantial relative to losses in other years?",
        "weight": 0.8,
        "favors_business_if_yes": True,
    },
    {
        "id": "financial_status",
        "question": "Do you depend on this income for your livelihood (i.e., no significant other income)?",
        "weight": 1.0,
        "favors_business_if_yes": True,
    },
    {
        "id": "personal_pleasure",
        "question": "Is the activity primarily for personal enjoyment or recreation? (Reverse: yes points toward HOBBY)",
        "weight": 1.0,
        "favors_business_if_yes": False,
    },
]


def classify_from_quiz(answers: dict[str, bool]) -> tuple[Classification, float, list[str]]:
    """
    Run the IRC §183 9-factor test. Returns (classification, score 0-1, rationale list).

    Scoring: weighted average. A score > 0.6 → DEALER (business), 0.35–0.6 → INVESTOR,
    below 0.35 → HOBBY. Investor sits in the middle: enough seriousness to be capital-asset
    treatment but not active enough to be a Schedule C trade-or-business.
    """
    total_weight = sum(f["weight"] for f in HOBBY_QUIZ_FACTORS)
    score = 0.0
    rationale: list[str] = []

    for factor in HOBBY_QUIZ_FACTORS:
        ans = answers.get(factor["id"], False)
        favors_business = factor["favors_business_if_yes"]
        # If the answer aligns with "favors business," award weight points.
        aligns_business = ans if favors_business else (not ans)
        if aligns_business:
            score += factor["weight"]
            rationale.append(f"+ {factor['id']}: favors business")
        else:
            rationale.append(f"– {factor['id']}: favors hobby/investor")

    normalized = score / total_weight

    if normalized >= 0.60:
        classification = Classification.DEALER
    elif normalized >= 0.35:
        classification = Classification.INVESTOR
    else:
        classification = Classification.HOBBY

    return classification, normalized, rationale


# ---------------------------------------------------------------------------
# Tax-loss harvesting opportunities
# ---------------------------------------------------------------------------


def loss_harvest_suggestions(
    transactions: Sequence[Transaction],
    open_lots_with_unrealized: Iterable[tuple[str, float]] | None = None,
) -> list[dict]:
    """
    Look for harvestable losses. Collectibles aren't subject to §1091 wash sale.
    """
    suggestions: list[dict] = []

    loss_txns = [t for t in transactions if t.gain_loss < 0]
    gain_txns = [t for t in transactions if t.gain_loss > 0]

    realized_losses = sum(-t.gain_loss for t in loss_txns)
    realized_gains = sum(t.gain_loss for t in gain_txns)

    if realized_gains > realized_losses and realized_gains > 0:
        offset_needed = realized_gains - realized_losses
        suggestions.append({
            "type": "harvest_to_offset",
            "headline": f"You have ${offset_needed:,.2f} of net gains uncovered by losses.",
            "detail": (
                "Selling a card at a loss before year-end will offset gains dollar-for-dollar. "
                "Collectibles aren't subject to wash-sale rules (IRC §1091), so you may "
                "immediately repurchase the same card and reset your basis lower without a 31-day wait."
            ),
        })

    if realized_losses > realized_gains:
        excess = realized_losses - realized_gains
        ordinary_offset = min(3_000, excess)
        carryover = max(0, excess - 3_000)
        suggestions.append({
            "type": "excess_loss",
            "headline": f"You have ${excess:,.2f} of losses beyond gains.",
            "detail": (
                f"${ordinary_offset:,.2f} can offset ordinary income this year. "
                + (f"${carryover:,.2f} carries forward indefinitely." if carryover else "")
            ),
        })

    return suggestions


# ---------------------------------------------------------------------------
# Charitable donation deduction (IRC §170)
# ---------------------------------------------------------------------------


def charitable_deduction(
    donations: Sequence[Transaction],
    adjusted_gross_income: float,
) -> dict:
    """
    Compute the federal charitable contribution deduction from donated cards.

    Rules:
      - LTCG property (held >1 year, would have been LT capital gain): deduct FMV,
        limited to 30% of AGI (IRC §170(b)(1)(C)).
      - Ordinary-income / short-term property: deduct cost basis only, limited
        to 50% of AGI (IRC §170(e)(1)(A); §170(b)(1)(A)).
      - SPECIAL RULE for tangible personal property under §170(e)(1)(B)(i):
        if the donee's use is UNRELATED to its exempt function (e.g., charity
        sells the card at auction), the deduction is reduced to BASIS regardless
        of holding period. Cards donated to most charities → unrelated use → basis only.
      - 5-year carryforward for excess (IRC §170(d)(1)).
    """
    fmv_total = 0.0
    basis_total = 0.0
    deduction_before_limit = 0.0
    notes: list[str] = []

    for d in donations:
        fmv = d.fmv_at_donation or d.sale_price or 0.0
        basis = d.total_basis
        fmv_total += fmv
        basis_total += basis

        # Determine holding period — re-use is_long_term
        long_held = d.is_long_term
        unrelated_use = d.donee_unrelated_use

        if long_held and not unrelated_use:
            # Related-use LTCG tangible personal property: FMV deduction
            deduction_before_limit += fmv
        elif long_held and unrelated_use:
            # Unrelated-use rule: §170(e)(1)(B)(i) → basis only
            deduction_before_limit += basis
        else:
            # Short-term or ordinary-income property: basis only
            deduction_before_limit += basis

    # AGI percentage limit. For tangible personal property the most permissive
    # limit is 30% of AGI when FMV deduction is allowed; otherwise 50% of AGI.
    # We apply the 30% limit broadly because cards are tangible personal property.
    agi = max(0.0, adjusted_gross_income)
    limit = 0.30 * agi
    allowed = min(deduction_before_limit, limit)
    carryforward = max(0.0, deduction_before_limit - allowed)

    if carryforward > 0:
        notes.append(
            f"${carryforward:,.2f} charitable contribution carries forward up to 5 years "
            f"(IRC §170(d)(1))."
        )

    return {
        "fmv_total": round(fmv_total, 2),
        "basis_total": round(basis_total, 2),
        "computed_deduction": round(deduction_before_limit, 2),
        "agi_limit": round(limit, 2),
        "allowed": round(allowed, 2),
        "carryforward": round(carryforward, 2),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Break spot allocation
# ---------------------------------------------------------------------------


def allocate_break_spot(spot_price: float, cards: list[dict]) -> list[dict]:
    """
    Allocate the cost basis of a break spot across the cards received.

    `cards` is a list of {"name": str, "fmv": float}. Returns each card with
    "allocated_basis" computed proportional to FMV.

    Example:
      spot $50, three cards FMV [$100, $30, $20] (total $150)
      → allocated bases [$33.33, $10.00, $6.67]
    """
    total_fmv = sum(c.get("fmv", 0.0) for c in cards)
    out = []
    if total_fmv <= 0:
        # Fall back to even split
        even = spot_price / max(1, len(cards))
        return [{"name": c.get("name", ""), "fmv": c.get("fmv", 0.0),
                 "allocated_basis": round(even, 2)} for c in cards]
    for c in cards:
        fmv = c.get("fmv", 0.0)
        allocated = spot_price * (fmv / total_fmv) if total_fmv else 0.0
        out.append({
            "name": c.get("name", ""),
            "fmv": round(fmv, 2),
            "allocated_basis": round(allocated, 2),
        })
    return out


# ---------------------------------------------------------------------------
# Quarterly estimated tax (Form 1040-ES)
# ---------------------------------------------------------------------------


# Per IRC §6654: safe harbor is 100% of prior year tax (110% if AGI > $150k).
SAFE_HARBOR_HIGH_AGI = 150_000
SAFE_HARBOR_RATE_NORMAL = 1.00
SAFE_HARBOR_RATE_HIGH = 1.10
ESTIMATED_PENALTY_MIN_OWED = 1_000


def quarterly_estimated_tax(
    estimated_annual_tax: float,
    prior_year_tax: float,
    prior_year_agi: float,
    withholding_paid: float = 0.0,
    year: int = 2025,
) -> dict:
    """
    Compute quarterly estimated tax payments (Form 1040-ES) and underpayment-risk flag.

    Returns dict with: payments_required, per_quarter, safe_harbor_amount,
    method_used, penalty_risk (bool), and a list of due dates.
    """
    safe_harbor_rate = SAFE_HARBOR_RATE_HIGH if prior_year_agi > SAFE_HARBOR_HIGH_AGI else SAFE_HARBOR_RATE_NORMAL
    prior_year_safe = prior_year_tax * safe_harbor_rate
    current_year_safe = estimated_annual_tax * 0.90

    # IRS lets you use the LOWER of the two safe harbor amounts as your target
    safe_harbor_target = min(prior_year_safe, current_year_safe) if prior_year_tax > 0 else current_year_safe
    method_used = "prior_year_110%" if (prior_year_safe <= current_year_safe and prior_year_agi > SAFE_HARBOR_HIGH_AGI) \
                  else "prior_year_100%" if prior_year_safe <= current_year_safe \
                  else "current_year_90%"

    required = max(0.0, safe_harbor_target - withholding_paid)
    per_quarter = required / 4.0

    # Underpayment penalty risk: owe > $1,000 and withholding < safe harbor
    penalty_risk = (estimated_annual_tax - withholding_paid) > ESTIMATED_PENALTY_MIN_OWED \
                   and withholding_paid < safe_harbor_target

    # 2025 due dates
    due_dates = [
        f"{year}-04-15",
        f"{year}-06-16",  # 6/15 falls on Sunday in 2025 → 6/16
        f"{year}-09-15",
        f"{year+1}-01-15",
    ]

    return {
        "estimated_annual_tax": round(estimated_annual_tax, 2),
        "prior_year_tax": round(prior_year_tax, 2),
        "withholding_paid": round(withholding_paid, 2),
        "safe_harbor_amount": round(safe_harbor_target, 2),
        "safe_harbor_method": method_used,
        "required_estimated_payments": round(required, 2),
        "per_quarter": round(per_quarter, 2),
        "due_dates": due_dates,
        "penalty_risk": penalty_risk,
        "notes": [
            f"Safe harbor rate: {int(safe_harbor_rate*100)}% of prior year tax "
            f"({'AGI > $150k → 110%' if prior_year_agi > SAFE_HARBOR_HIGH_AGI else 'standard 100%'}).",
            "Pay equal installments of the per-quarter amount on each due date "
            "to avoid the §6654 underpayment penalty.",
            "Use the annualized-income installment method (Form 2210 Schedule AI) "
            "if income is lumpy across quarters.",
        ],
    }


# ---------------------------------------------------------------------------
# §1031 like-kind exchange misconception
# ---------------------------------------------------------------------------


LIKE_KIND_EXCHANGE_NOTE = (
    "IRC §1031 like-kind exchanges: COLLECTIBLES ARE NOT ELIGIBLE. "
    "The TCJA (P.L. 115-97, §13303, effective 2018) limited §1031 to REAL PROPERTY only. "
    "Cards, comics, art, coins, and all tangible personal property are excluded. "
    "Any swap or trade of cards is a fully taxable disposition at fair market value. "
    "This is a frequent misconception — there is no way to defer gain by trading one card for another."
)


# ---------------------------------------------------------------------------
# Alternative Minimum Tax (Form 6251) — 2025 figures
# ---------------------------------------------------------------------------

AMT_EXEMPTION_2025 = {
    "single": 88_100,
    "mfj":    137_000,
    "mfs":    68_500,
    "hoh":    88_100,
}
# Exemption phase-out: 25% reduction over the threshold; fully phased out
# when AMTI exceeds threshold + 4 × exemption.
AMT_EXEMPTION_PHASEOUT_START_2025 = {
    "single": 626_350,
    "mfj":    1_252_700,
    "mfs":    626_350,
    "hoh":    626_350,
}
AMT_BREAKPOINT_26_28 = 239_100  # $119,550 if MFS
AMT_RATE_LOW = 0.26
AMT_RATE_HIGH = 0.28


def amt_calculation(
    regular_taxable_income: float,
    collectible_lt_gain: float,
    filing_status: FilingStatus,
    regular_tax: float,
) -> dict:
    """
    Tentative minimum tax (Form 6251). Returns dict with AMTI, exemption,
    AMT amount, and whether AMT applies (AMT > regular tax).

    Collectibles 28% rate gain is included in AMTI but the 28% federal rate
    is preserved (per IRC §55(b)(3)). We compute the simplified TMT:
      TMT = 28%-rate-gain tax + AMT on remaining AMTI.
    """
    fs = filing_status.value
    exemption = AMT_EXEMPTION_2025[fs]
    phase_start = AMT_EXEMPTION_PHASEOUT_START_2025[fs]
    breakpoint = AMT_BREAKPOINT_26_28 / 2 if fs == "mfs" else AMT_BREAKPOINT_26_28

    amti = max(0.0, regular_taxable_income) + max(0.0, collectible_lt_gain)

    # Exemption phase-out
    if amti > phase_start:
        excess = amti - phase_start
        exemption = max(0.0, exemption - 0.25 * excess)

    amti_after_exemption = max(0.0, amti - exemption)

    # Carve out collectibles gain — taxed at the 28% rate inside AMT
    coll_for_amt = min(collectible_lt_gain, amti_after_exemption)
    other_amti = amti_after_exemption - coll_for_amt

    # 26/28% on the "other" AMTI portion
    if other_amti <= breakpoint:
        amt_on_other = other_amti * AMT_RATE_LOW
    else:
        amt_on_other = breakpoint * AMT_RATE_LOW + (other_amti - breakpoint) * AMT_RATE_HIGH

    tmt = amt_on_other + coll_for_amt * COLLECTIBLES_MAX_LTCG_RATE
    amt_owed = max(0.0, tmt - regular_tax)
    applies = amt_owed > 0

    return {
        "amti": round(amti, 2),
        "exemption_used": round(exemption, 2),
        "amti_after_exemption": round(amti_after_exemption, 2),
        "tentative_minimum_tax": round(tmt, 2),
        "regular_tax_for_comparison": round(regular_tax, 2),
        "amt_owed": round(amt_owed, 2),
        "applies": applies,
        "note": (
            "AMT applies — your tentative minimum tax exceeds regular tax. "
            "File Form 6251 and pay the difference."
            if applies else
            "AMT does not apply. Your regular tax is higher than tentative minimum tax."
        ),
    }


# ---------------------------------------------------------------------------
# Self-Employment tax (Schedule SE) — broken out for dealers
# ---------------------------------------------------------------------------

SE_SS_WAGE_BASE_2025 = 168_600
SE_RATE_SS = 0.124       # Social Security (12.4%)
SE_RATE_MEDICARE = 0.029  # Medicare (2.9%)
SE_RATE_ADDL_MEDICARE = 0.009  # Additional Medicare 0.9% over $200k single / $250k MFJ
SE_ADDL_MEDICARE_THRESHOLD = {"single": 200_000, "mfj": 250_000, "mfs": 125_000, "hoh": 200_000}
SE_NET_EARNINGS_FACTOR = 0.9235  # §1402(a)


def self_employment_tax(net_se_earnings: float, filing_status: FilingStatus) -> dict:
    """
    Schedule SE computation.

    Returns dict with SE earnings base, SS portion, Medicare portion,
    additional 0.9% Medicare (if applicable), total SE tax, and the
    deductible-half adjustment (Schedule 1 line 15 = ½ of regular SE tax).
    """
    se_base = max(0.0, net_se_earnings) * SE_NET_EARNINGS_FACTOR
    ss_tax = min(se_base, SE_SS_WAGE_BASE_2025) * SE_RATE_SS
    medicare_tax = se_base * SE_RATE_MEDICARE

    threshold = SE_ADDL_MEDICARE_THRESHOLD[filing_status.value]
    addl_medicare = max(0.0, se_base - threshold) * SE_RATE_ADDL_MEDICARE

    total = ss_tax + medicare_tax + addl_medicare
    deductible_half = (ss_tax + medicare_tax) / 2  # additional 0.9% Medicare is NOT deductible

    return {
        "net_se_earnings": round(net_se_earnings, 2),
        "se_base_92_35pct": round(se_base, 2),
        "social_security_tax": round(ss_tax, 2),
        "medicare_tax": round(medicare_tax, 2),
        "additional_medicare_tax": round(addl_medicare, 2),
        "total_se_tax": round(total, 2),
        "deductible_half_schedule_1": round(deductible_half, 2),
    }


# ---------------------------------------------------------------------------
# §199A Qualified Business Income deduction (dealers only)
# ---------------------------------------------------------------------------

QBI_THRESHOLD_2025 = {"single": 197_300, "mfj": 394_600, "mfs": 197_300, "hoh": 197_300}
QBI_PHASEIN_RANGE = {"single": 50_000, "mfj": 100_000, "mfs": 50_000, "hoh": 50_000}


def qbi_deduction(
    qualified_business_income: float,
    taxable_income_before_qbi: float,
    net_capital_gain: float,
    filing_status: FilingStatus,
    w2_wages_paid: float = 0.0,
    qualified_property_basis: float = 0.0,
    is_sstb: bool = False,
) -> dict:
    """
    §199A QBI deduction. Card dealing is NOT a specified service trade or
    business (SSTB), so the deduction is generally available regardless of
    income, subject to the W-2 wage / UBIA-of-qualified-property limitation.
    """
    if qualified_business_income <= 0:
        return {"deduction": 0.0, "tentative": 0.0, "applies": False,
                "note": "No QBI deduction — qualified business income is zero or negative."}

    threshold = QBI_THRESHOLD_2025[filing_status.value]
    phasein_top = threshold + QBI_PHASEIN_RANGE[filing_status.value]
    ti = max(0.0, taxable_income_before_qbi)

    # Tentative 20% deduction
    tentative = 0.20 * qualified_business_income

    # Taxable-income limit (cannot exceed 20% of (TI - net capital gain))
    ti_limit = 0.20 * max(0.0, ti - max(0.0, net_capital_gain))

    notes: list[str] = []

    if ti <= threshold:
        deduction = min(tentative, ti_limit)
        notes.append("Below QBI threshold — no W-2 wage limit applies; SSTB rules don't kick in.")
    else:
        # SSTB above threshold = phased-out. Card dealing is NOT SSTB.
        if is_sstb and ti >= phasein_top:
            return {"deduction": 0.0, "tentative": round(tentative, 2),
                    "applies": False,
                    "note": "Above SSTB threshold — no deduction for specified service trades."}

        # Apply W-2 wages / property limitation
        # Limit = greater of (50% of W-2 wages) OR (25% of W-2 wages + 2.5% of UBIA)
        wage_limit_a = 0.50 * w2_wages_paid
        wage_limit_b = 0.25 * w2_wages_paid + 0.025 * qualified_property_basis
        wage_limit = max(wage_limit_a, wage_limit_b)

        if ti >= phasein_top:
            deduction = min(tentative, wage_limit, ti_limit)
            notes.append(f"Above full phase-in: limited to greater of 50% W-2 wages or 25% wages + 2.5% UBIA = ${wage_limit:,.2f}.")
        else:
            # Phase-in range: blend
            pct_phased = (ti - threshold) / QBI_PHASEIN_RANGE[filing_status.value]
            limited = min(tentative, wage_limit, ti_limit)
            deduction = tentative - (tentative - limited) * pct_phased
            notes.append(f"In phase-in range ({pct_phased*100:.0f}% phased). Blended deduction.")

    deduction = max(0.0, min(deduction, ti_limit))
    return {
        "deduction": round(deduction, 2),
        "tentative": round(tentative, 2),
        "taxable_income_limit": round(ti_limit, 2),
        "applies": deduction > 0,
        "is_sstb": is_sstb,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Net Operating Loss (NOL) — post-TCJA rules
# ---------------------------------------------------------------------------


def nol_carryforward(
    current_year_business_loss: float,
    prior_year_nol_carryforward: float = 0.0,
    current_year_taxable_income: float = 0.0,
) -> dict:
    """
    Track NOL under post-TCJA rules: no carryback, indefinite carryforward,
    deduction limited to 80% of taxable income in the carryforward year.
    """
    total_available = prior_year_nol_carryforward + max(0.0, -current_year_business_loss if current_year_business_loss < 0 else 0)

    eighty_pct_limit = 0.80 * max(0.0, current_year_taxable_income)
    used_this_year = min(prior_year_nol_carryforward, eighty_pct_limit)
    new_loss_generated = max(0.0, -current_year_business_loss) if current_year_business_loss < 0 else 0.0
    carryforward_to_next = (prior_year_nol_carryforward - used_this_year) + new_loss_generated

    return {
        "prior_year_carryforward": round(prior_year_nol_carryforward, 2),
        "current_year_business_loss": round(min(0.0, current_year_business_loss), 2),
        "eighty_pct_taxable_income_limit": round(eighty_pct_limit, 2),
        "nol_used_this_year": round(used_this_year, 2),
        "new_loss_added": round(new_loss_generated, 2),
        "carryforward_to_next_year": round(carryforward_to_next, 2),
        "note": "Post-TCJA: NOLs carry forward indefinitely. No carryback (except certain farming/insurance). "
                "Limited to 80% of taxable income in the year of deduction.",
    }


# ---------------------------------------------------------------------------
# Kiddie tax (IRC §1(g))
# ---------------------------------------------------------------------------

KIDDIE_TAX_UNEARNED_FLOOR_2025 = 1_350    # first $1,350 untaxed (std deduction)
KIDDIE_TAX_PARENT_RATE_THRESHOLD_2025 = 2_700  # over $2,700: parent rate


def kiddie_tax_check(
    child_unearned_income: float,
    child_is_dependent_under_24: bool,
    parent_marginal_rate: float,
    child_marginal_rate: float,
) -> dict:
    """
    Determine if the kiddie tax applies and how much extra tax is owed.

    Applies if child:
      - is under 18, or
      - is 18 and earned income did not exceed half of support, or
      - is 19–23, a full-time student, and earned income did not exceed half of support.
    """
    if not child_is_dependent_under_24 or child_unearned_income <= KIDDIE_TAX_PARENT_RATE_THRESHOLD_2025:
        return {
            "applies": False,
            "extra_tax_at_parent_rate": 0.0,
            "note": "Kiddie tax does NOT apply (either child is not a dependent under 24, "
                    "or unearned income is below the $2,700 threshold).",
        }

    parent_rate_amount = child_unearned_income - KIDDIE_TAX_PARENT_RATE_THRESHOLD_2025
    # Roughly: tax at parent rate minus what would have been at child rate
    extra = parent_rate_amount * (parent_marginal_rate - child_marginal_rate)
    return {
        "applies": True,
        "income_at_parent_rate": round(parent_rate_amount, 2),
        "extra_tax_at_parent_rate": round(max(0.0, extra), 2),
        "note": "Form 8615 required. Unearned income over $2,700 is taxed at the parent's marginal rate.",
    }


# ---------------------------------------------------------------------------
# Form 8949 preview — required alongside Schedule D
# ---------------------------------------------------------------------------


def form_8949_preview(transactions: Sequence[Transaction]) -> dict:
    """
    Build a Form 8949 preview.

    Three short-term boxes and three long-term boxes:
      Box A / D: 1099-B received with basis reported to IRS
      Box B / E: 1099-B received with basis NOT reported to IRS
      Box C / F: no 1099-B received

    Card sales generally fall into Box C (short-term) or Box F (long-term)
    because brokers do not issue 1099-B for collectibles. eBay and Whatnot
    issue 1099-K (not 1099-B).
    """
    boxes = {
        "short_term": {"A_with_basis": [], "B_no_basis": [], "C_no_1099b": []},
        "long_term":  {"D_with_basis": [], "E_no_basis": [], "F_no_1099b": []},
    }

    for t in transactions:
        if t.is_donation:
            continue
        # Default everything to "no 1099-B" — that's the right Box for cards
        target_box = "F_no_1099b" if t.is_long_term else "C_no_1099b"
        target_section = "long_term" if t.is_long_term else "short_term"

        date_acq = ""
        if t.acquisition_type == AcquisitionType.INHERITANCE:
            date_acq = "INHERITED"
        elif t.acquisition_type == AcquisitionType.GIFT and t.gift_date:
            date_acq = t.gift_date.isoformat()
        else:
            date_acq = t.purchase_date.isoformat() if t.purchase_date else ""

        # Code adjustments per Form 8949 instructions
        code = ""
        if t.acquisition_type == AcquisitionType.PERSONAL_COLLECTION:
            code = "L"  # Loss not allowed on personal-use property
        elif t.acquisition_type == AcquisitionType.GIFT:
            code = "B"  # Basis adjustment

        boxes[target_section][target_box].append({
            "description": t.item_title,
            "date_acquired": date_acq,
            "date_sold": t.sale_date.isoformat(),
            "proceeds": round(t.net_proceeds, 2),
            "basis": round(t.total_basis, 2),
            "code": code,
            "adjustment": 0.0,
            "gain_loss": round(t.gain_loss, 2),
        })

    def _totals(section):
        return {
            box: {
                "rows": rows,
                "totals": {
                    "proceeds": round(sum(r["proceeds"] for r in rows), 2),
                    "basis": round(sum(r["basis"] for r in rows), 2),
                    "gain_loss": round(sum(r["gain_loss"] for r in rows), 2),
                },
            }
            for box, rows in section.items()
        }

    return {
        "short_term": _totals(boxes["short_term"]),
        "long_term":  _totals(boxes["long_term"]),
        "note": (
            "Most card sales go in Box C (short-term) or Box F (long-term) since brokers do not "
            "issue Form 1099-B for collectibles. eBay / Whatnot 1099-K is informational, not basis-reported."
        ),
    }


# ---------------------------------------------------------------------------
# Tax alerts — surfaced on the dashboard
# ---------------------------------------------------------------------------


def tax_alerts(summary, quarterly: dict | None = None) -> list[dict]:
    """Build short, actionable alerts to surface on the dashboard."""
    alerts: list[dict] = []

    if summary.disallowed_personal_use_losses > 0:
        alerts.append({
            "severity": "warning",
            "title": "Personal-use losses disallowed",
            "detail": f"${summary.disallowed_personal_use_losses:,.2f} in losses on personal-use cards aren't deductible under IRC §165(c).",
        })

    if summary.niit > 0:
        alerts.append({
            "severity": "info",
            "title": "NIIT applies",
            "detail": f"3.8% Net Investment Income Tax of ${summary.niit:,.2f} kicks in because your MAGI is over the threshold.",
        })

    if summary.net_long_term > 5_000 and summary.short_term_loss + summary.long_term_loss == 0:
        alerts.append({
            "severity": "tip",
            "title": "Loss-harvesting opportunity",
            "detail": (
                f"You have ${summary.net_long_term:,.2f} of unoffset gains. Selling losing cards before year-end "
                "would offset gains dollar-for-dollar. No wash-sale rule on collectibles."
            ),
        })

    if quarterly and quarterly.get("penalty_risk"):
        alerts.append({
            "severity": "warning",
            "title": "Underpayment penalty risk",
            "detail": (
                f"You owe more than $1,000 and are below the safe-harbor target "
                f"of ${quarterly['safe_harbor_amount']:,.2f}. "
                f"Make estimated payments of ${quarterly['per_quarter']:,.2f} each quarter "
                f"(next due {quarterly['due_dates'][0]})."
            ),
        })

    if summary.charitable_deduction_allowed > 0:
        alerts.append({
            "severity": "info",
            "title": "Charitable deduction available",
            "detail": f"${summary.charitable_deduction_allowed:,.2f} of donated-card deduction usable on Schedule A.",
        })

    return alerts
