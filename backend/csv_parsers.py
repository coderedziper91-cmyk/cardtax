"""
CSV parsers for eBay, Whatnot, and generic platforms.

Each parser returns list[ParsedSale] — a normalized intermediate that the API
can persist into the Transaction table. The user fills in cost basis afterward.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable


@dataclass
class ParsedSale:
    item_title: str
    sale_date: date
    sale_price: float
    platform_fees: float = 0.0
    shipping_charged: float = 0.0
    shipping_cost_out: float = 0.0
    platform_source: str = "unknown"
    notes: str = ""
    raw: dict = field(default_factory=dict)


def _money(value) -> float:
    """Convert '$1,234.56' / '(12.34)' / '-12.34' to a float."""
    if value is None:
        return 0.0
    s = str(value).strip()
    if not s or s.lower() in ("n/a", "na", "--", "-"):
        return 0.0
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").replace(" ", "")
    if s == "" or s == "-":
        return 0.0
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return -v if negative else v


def _parse_date(value) -> date | None:
    if not value:
        return None
    s = str(value).strip()
    formats = [
        "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%b %d, %Y", "%B %d, %Y",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%y",
        "%d-%b-%Y", "%b %d %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Try ISO with timezone
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def detect_platform(headers: list[str]) -> str:
    """Sniff CSV headers to guess the source platform."""
    lower = [h.strip().lower() for h in headers]
    joined = " | ".join(lower)

    if any("ebay" in h for h in lower) or "item title" in joined and "sold for" in joined:
        return "ebay"
    if any("transaction date" in h for h in lower) and any("sold for" in h or "buyer paid" in h for h in lower):
        return "ebay"
    if any("whatnot" in h for h in lower) or "show name" in joined:
        return "whatnot"
    if "lot title" in joined and "sale price" in joined:
        return "whatnot"
    if "comc" in joined or "consignment" in joined:
        return "comc"
    if "myslabs" in joined or ("slab" in joined and "asking" in joined):
        return "myslabs"
    if "tcgplayer" in joined or ("order number" in joined and "product name" in joined):
        return "tcgplayer"
    if "mercari" in joined or ("item id" in joined and "item name" in joined and "sold price" in joined):
        return "mercari"
    return "generic"


# ---------------------------------------------------------------------------
# eBay
# ---------------------------------------------------------------------------


EBAY_TITLE_CANDIDATES = ["Item title", "Title", "Listing title", "Item Title"]
EBAY_DATE_CANDIDATES = ["Sale date", "Transaction date", "Sold date", "Date sold", "Order date", "Sale Date"]
EBAY_PRICE_CANDIDATES = ["Sold for", "Item subtotal", "Sale price", "Gross amount", "Item price"]
EBAY_FEES_CANDIDATES = ["Total fees", "eBay fees", "Selling fees", "Final value fee", "Fees"]
EBAY_SHIP_CHARGED = ["Shipping and handling", "Shipping handling", "Buyer paid shipping", "Shipping charged"]
EBAY_SHIP_COST = ["Shipping label cost", "Postage cost", "Label cost", "Shipping cost"]
EBAY_NET = ["Net amount", "Total payout", "Payout"]


def _pick(row: dict, candidates: list[str]) -> str | None:
    """Find first matching key in a row dict (case-insensitive, trimmed)."""
    norm = {k.strip().lower(): k for k in row.keys() if k}
    for cand in candidates:
        if cand.lower() in norm:
            return row[norm[cand.lower()]]
    return None


def parse_ebay(text: str) -> list[ParsedSale]:
    """Parse an eBay seller CSV export."""
    sales: list[ParsedSale] = []

    # eBay exports often have preamble rows; find header row
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    header_idx = None
    for i, row in enumerate(rows):
        cells_lower = [c.strip().lower() for c in row]
        if any("item title" in c or "transaction date" in c or "sold for" in c for c in cells_lower):
            header_idx = i
            break
    if header_idx is None:
        return sales

    headers = [h.strip() for h in rows[header_idx]]
    for row in rows[header_idx + 1:]:
        if not any(c.strip() for c in row):
            continue
        rd = dict(zip(headers, row))
        title = _pick(rd, EBAY_TITLE_CANDIDATES)
        if not title or not title.strip():
            continue
        sale_date = _parse_date(_pick(rd, EBAY_DATE_CANDIDATES))
        if not sale_date:
            continue
        sales.append(ParsedSale(
            item_title=title.strip(),
            sale_date=sale_date,
            sale_price=_money(_pick(rd, EBAY_PRICE_CANDIDATES)),
            platform_fees=_money(_pick(rd, EBAY_FEES_CANDIDATES)),
            shipping_charged=_money(_pick(rd, EBAY_SHIP_CHARGED)),
            shipping_cost_out=_money(_pick(rd, EBAY_SHIP_COST)),
            platform_source="ebay",
            raw=rd,
        ))
    return sales


# ---------------------------------------------------------------------------
# Whatnot
# ---------------------------------------------------------------------------


WHATNOT_TITLE = ["Lot Title", "Item Name", "Product Name", "Lot title"]
WHATNOT_DATE = ["Sale Date", "Order Date", "Transaction Date", "Date"]
WHATNOT_PRICE = ["Sale Price", "Lot Price", "Sold For", "Price"]
WHATNOT_FEES = ["Whatnot Fee", "Platform Fee", "Seller Fee", "Total Fees", "Fees"]
WHATNOT_SHIP_CHARGED = ["Shipping Paid", "Shipping Charged", "Buyer Shipping"]
WHATNOT_SHIP_COST = ["Shipping Cost", "Label Cost"]


def parse_whatnot(text: str) -> list[ParsedSale]:
    sales: list[ParsedSale] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        title = _pick(row, WHATNOT_TITLE)
        if not title or not title.strip():
            continue
        sale_date = _parse_date(_pick(row, WHATNOT_DATE))
        if not sale_date:
            continue
        sales.append(ParsedSale(
            item_title=title.strip(),
            sale_date=sale_date,
            sale_price=_money(_pick(row, WHATNOT_PRICE)),
            platform_fees=_money(_pick(row, WHATNOT_FEES)),
            shipping_charged=_money(_pick(row, WHATNOT_SHIP_CHARGED)),
            shipping_cost_out=_money(_pick(row, WHATNOT_SHIP_COST)),
            platform_source="whatnot",
            raw=row,
        ))
    return sales


# ---------------------------------------------------------------------------
# COMC (Check Out My Cards) — consignment sales export
# ---------------------------------------------------------------------------


COMC_TITLE = ["Description", "Item", "Card", "Title", "Item Description"]
COMC_DATE = ["Sale Date", "Date Sold", "Date", "Transaction Date"]
COMC_PRICE = ["Sale Price", "Sold Price", "Net Sale", "Gross Sale", "Sale Amount"]
COMC_FEES = ["Commission", "COMC Fees", "Fees", "Seller Commission"]
COMC_SHIP_OUT = ["Shipping Cost", "Shipping", "Outbound Shipping"]


def parse_comc(text: str) -> list[ParsedSale]:
    sales: list[ParsedSale] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        title = _pick(row, COMC_TITLE)
        if not title or not title.strip():
            continue
        sale_date = _parse_date(_pick(row, COMC_DATE))
        if not sale_date:
            continue
        sales.append(ParsedSale(
            item_title=title.strip(),
            sale_date=sale_date,
            sale_price=_money(_pick(row, COMC_PRICE)),
            platform_fees=_money(_pick(row, COMC_FEES)),
            shipping_charged=0.0,
            shipping_cost_out=_money(_pick(row, COMC_SHIP_OUT)),
            platform_source="comc",
            raw=row,
        ))
    return sales


# ---------------------------------------------------------------------------
# MySlabs — graded-card marketplace export
# ---------------------------------------------------------------------------


MYSLABS_TITLE = ["Title", "Listing Title", "Item Name", "Slab"]
MYSLABS_DATE = ["Sold Date", "Sale Date", "Date Sold", "Order Date"]
MYSLABS_PRICE = ["Sold Price", "Sale Price", "Asking Price", "Price"]
MYSLABS_FEES = ["MySlabs Fee", "Marketplace Fee", "Fees", "Commission"]
MYSLABS_SHIP_CHARGED = ["Shipping Charged", "Shipping Paid"]
MYSLABS_SHIP_OUT = ["Shipping Cost", "Label Cost"]


def parse_myslabs(text: str) -> list[ParsedSale]:
    sales: list[ParsedSale] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        title = _pick(row, MYSLABS_TITLE)
        if not title or not title.strip():
            continue
        sale_date = _parse_date(_pick(row, MYSLABS_DATE))
        if not sale_date:
            continue
        sales.append(ParsedSale(
            item_title=title.strip(),
            sale_date=sale_date,
            sale_price=_money(_pick(row, MYSLABS_PRICE)),
            platform_fees=_money(_pick(row, MYSLABS_FEES)),
            shipping_charged=_money(_pick(row, MYSLABS_SHIP_CHARGED)),
            shipping_cost_out=_money(_pick(row, MYSLABS_SHIP_OUT)),
            platform_source="myslabs",
            raw=row,
        ))
    return sales


# ---------------------------------------------------------------------------
# TCGPlayer seller export
# ---------------------------------------------------------------------------


TCG_TITLE = ["Product Name", "Item Name", "Product", "Title"]
TCG_DATE = ["Order Date", "Date", "Transaction Date", "Sale Date"]
TCG_PRICE = ["Item Total", "Sale Price", "Price", "Item Price", "Sold For"]
TCG_FEES = ["Commission Fee", "Seller Fee", "Fees", "TCGplayer Fee"]
TCG_SHIP_CHARGED = ["Shipping Charged", "Buyer Paid Shipping"]
TCG_SHIP_OUT = ["Shipping Cost", "Postage"]


def parse_tcgplayer(text: str) -> list[ParsedSale]:
    sales: list[ParsedSale] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        title = _pick(row, TCG_TITLE)
        if not title or not title.strip():
            continue
        sale_date = _parse_date(_pick(row, TCG_DATE))
        if not sale_date:
            continue
        sales.append(ParsedSale(
            item_title=title.strip(),
            sale_date=sale_date,
            sale_price=_money(_pick(row, TCG_PRICE)),
            platform_fees=_money(_pick(row, TCG_FEES)),
            shipping_charged=_money(_pick(row, TCG_SHIP_CHARGED)),
            shipping_cost_out=_money(_pick(row, TCG_SHIP_OUT)),
            platform_source="tcgplayer",
            raw=row,
        ))
    return sales


# ---------------------------------------------------------------------------
# Mercari sales report
# ---------------------------------------------------------------------------


MERCARI_TITLE = ["Item Name", "Item Title", "Product", "Title"]
MERCARI_DATE = ["Sold Date", "Sale Date", "Date Sold", "Order Date"]
MERCARI_PRICE = ["Sold Price", "Item Price", "Sale Price", "Price"]
MERCARI_FEES = ["Selling Fee", "Mercari Fee", "Fees", "Marketplace Fee"]
MERCARI_SHIP_CHARGED = ["Shipping Charged", "Buyer Paid Shipping"]
MERCARI_SHIP_OUT = ["Shipping Cost", "Label Cost"]


def parse_mercari(text: str) -> list[ParsedSale]:
    sales: list[ParsedSale] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        title = _pick(row, MERCARI_TITLE)
        if not title or not title.strip():
            continue
        sale_date = _parse_date(_pick(row, MERCARI_DATE))
        if not sale_date:
            continue
        sales.append(ParsedSale(
            item_title=title.strip(),
            sale_date=sale_date,
            sale_price=_money(_pick(row, MERCARI_PRICE)),
            platform_fees=_money(_pick(row, MERCARI_FEES)),
            shipping_charged=_money(_pick(row, MERCARI_SHIP_CHARGED)),
            shipping_cost_out=_money(_pick(row, MERCARI_SHIP_OUT)),
            platform_source="mercari",
            raw=row,
        ))
    return sales


# ---------------------------------------------------------------------------
# Generic CSV with caller-supplied column mapping
# ---------------------------------------------------------------------------


def parse_generic(text: str, mapping: dict[str, str]) -> list[ParsedSale]:
    """
    Generic parser.
    `mapping` keys: item_title, sale_date, sale_price, platform_fees,
                    shipping_charged, shipping_cost_out, platform_source
    Values are the CSV column names to read.
    """
    sales: list[ParsedSale] = []
    reader = csv.DictReader(io.StringIO(text))
    platform = mapping.get("platform_source", "generic")
    for row in reader:
        title_col = mapping.get("item_title")
        date_col = mapping.get("sale_date")
        price_col = mapping.get("sale_price")
        if not title_col or not date_col or not price_col:
            continue
        title = row.get(title_col, "").strip()
        sd = _parse_date(row.get(date_col))
        if not title or not sd:
            continue
        sales.append(ParsedSale(
            item_title=title,
            sale_date=sd,
            sale_price=_money(row.get(price_col)),
            platform_fees=_money(row.get(mapping.get("platform_fees", ""))) if mapping.get("platform_fees") else 0.0,
            shipping_charged=_money(row.get(mapping.get("shipping_charged", ""))) if mapping.get("shipping_charged") else 0.0,
            shipping_cost_out=_money(row.get(mapping.get("shipping_cost_out", ""))) if mapping.get("shipping_cost_out") else 0.0,
            platform_source=platform,
            raw=row,
        ))
    return sales


# ---------------------------------------------------------------------------
# Auto-dispatch
# ---------------------------------------------------------------------------


def parse_csv(text: str, platform_hint: str | None = None, mapping: dict | None = None) -> tuple[list[ParsedSale], str]:
    """Top-level entry. Returns (sales, detected_platform)."""
    # Read first non-empty line for detection
    reader = csv.reader(io.StringIO(text))
    headers: list[str] = []
    for row in reader:
        if any(c.strip() for c in row):
            headers = row
            break

    platform = platform_hint or detect_platform(headers)
    if platform == "ebay":
        return parse_ebay(text), "ebay"
    if platform == "whatnot":
        return parse_whatnot(text), "whatnot"
    if platform == "comc":
        return parse_comc(text), "comc"
    if platform == "myslabs":
        return parse_myslabs(text), "myslabs"
    if platform == "tcgplayer":
        return parse_tcgplayer(text), "tcgplayer"
    if platform == "mercari":
        return parse_mercari(text), "mercari"
    if platform == "generic" and mapping:
        return parse_generic(text, mapping), "generic"

    # Fallback: headers preview for column mapping UI
    return [], platform


def csv_headers(text: str) -> list[str]:
    """Return the list of column names in a CSV (skipping blank preamble rows)."""
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if any(c.strip() for c in row):
            return [c.strip() for c in row]
    return []
