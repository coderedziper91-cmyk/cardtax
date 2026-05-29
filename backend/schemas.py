"""Pydantic request/response schemas."""

from __future__ import annotations

from datetime import date
from typing import Optional, List, Dict
from pydantic import BaseModel, Field


class TransactionIn(BaseModel):
    item_title: str
    sale_date: date
    sale_price: float = 0.0
    platform_fees: float = 0.0
    shipping_charged: float = 0.0
    shipping_cost_out: float = 0.0
    purchase_date: Optional[date] = None
    purchase_price: float = 0.0
    grading_fees: float = 0.0
    other_basis_costs: float = 0.0
    platform_source: str = "manual"
    notes: str = ""
    image_path: str = ""
    card_category: str = ""
    # Acquisition details
    acquisition_type: str = "purchase"
    donor_basis: Optional[float] = None
    gift_date: Optional[date] = None
    fmv_at_gift: Optional[float] = None
    donor_holding_period_start: Optional[date] = None
    date_of_death: Optional[date] = None
    fmv_at_death: Optional[float] = None
    break_spot_price: Optional[float] = None
    break_total_fmv: Optional[float] = None
    donation_date: Optional[date] = None
    fmv_at_donation: Optional[float] = None
    donee_organization: str = ""
    donee_unrelated_use: bool = True


class TransactionOut(TransactionIn):
    id: int
    net_proceeds: float
    total_basis: float
    gain_loss: float
    holding_period_days: Optional[int] = None
    is_long_term: bool


class TransactionPatch(BaseModel):
    item_title: Optional[str] = None
    sale_date: Optional[date] = None
    sale_price: Optional[float] = None
    platform_fees: Optional[float] = None
    shipping_charged: Optional[float] = None
    shipping_cost_out: Optional[float] = None
    purchase_date: Optional[date] = None
    purchase_price: Optional[float] = None
    grading_fees: Optional[float] = None
    other_basis_costs: Optional[float] = None
    notes: Optional[str] = None
    acquisition_type: Optional[str] = None
    donor_basis: Optional[float] = None
    gift_date: Optional[date] = None
    fmv_at_gift: Optional[float] = None
    donor_holding_period_start: Optional[date] = None
    date_of_death: Optional[date] = None
    fmv_at_death: Optional[float] = None
    break_spot_price: Optional[float] = None
    break_total_fmv: Optional[float] = None
    donation_date: Optional[date] = None
    fmv_at_donation: Optional[float] = None
    donee_organization: Optional[str] = None
    donee_unrelated_use: Optional[bool] = None


class SettingsIn(BaseModel):
    tax_year: int = 2025
    filing_status: str = "single"
    state: str = ""
    city: str = ""
    ordinary_income_estimate: float = 0.0
    classification: str = "investor"
    lot_method: str = "fifo"
    prior_year_tax: float = 0.0
    prior_year_agi: float = 0.0
    withholding_paid: float = 0.0
    is_kiddie_filer: bool = False
    parent_marginal_rate: float = 0.22
    nol_carryforward: float = 0.0


class QuizSubmit(BaseModel):
    answers: Dict[str, bool]


class CsvMapping(BaseModel):
    item_title: str
    sale_date: str
    sale_price: str
    platform_fees: Optional[str] = None
    shipping_charged: Optional[str] = None
    shipping_cost_out: Optional[str] = None
    platform_source: str = "generic"


class BreakAllocationCard(BaseModel):
    name: str = ""
    fmv: float = 0.0


class BreakAllocationRequest(BaseModel):
    spot_price: float
    cards: List[BreakAllocationCard]


class WaitlistIn(BaseModel):
    email: str
    platforms: List[str] = Field(default_factory=list)
    monthly_volume: str = ""
    referrer: str = ""


class ScanResult(BaseModel):
    """Returned from /api/scan after the vision LLM identifies a card."""
    source: str
    error: Optional[str] = None
    card_name: str = ""
    player_name: str = ""
    year: Optional[int] = None
    set_name: str = ""
    card_number: str = ""
    parallel_variant: Optional[str] = None
    sport_or_game: str = "other"
    estimated_value_usd: Optional[float] = None
    confidence: float = 0.0
    notes: str = ""
    image_path: str = ""
