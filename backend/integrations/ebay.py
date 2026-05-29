"""eBay OAuth + Sell Fulfillment API integration.

OAuth — Authorization Code grant with refresh token. Configure via env vars:
    EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_REDIRECT_URI
    EBAY_ENV  = "production" | "sandbox"   (default: production)
    EBAY_RU_NAME = optional, eBay's "RuName" for redirect URI replacement

To pull completed sales we hit the Sell Fulfillment API:
    GET /sell/fulfillment/v1/order?filter=...
which returns orders + line items + buyer postal info + monetary breakdowns.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional
from urllib.parse import urlencode

import httpx

from ..csv_parsers import ParsedSale


EBAY_ENV = os.environ.get("EBAY_ENV", "production").lower()
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
EBAY_REDIRECT_URI = os.environ.get(
    "EBAY_REDIRECT_URI", "http://localhost:8000/api/integrations/ebay/callback"
)
# eBay accepts a "RuName" (their alias for redirect URI) in the auth URL.
# If unset, fall back to the raw redirect URI.
EBAY_RU_NAME = os.environ.get("EBAY_RU_NAME", "").strip()

if EBAY_ENV == "sandbox":
    EBAY_AUTH_URL = "https://auth.sandbox.ebay.com/oauth2/authorize"
    EBAY_TOKEN_URL = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    EBAY_API_BASE = "https://api.sandbox.ebay.com"
else:
    EBAY_AUTH_URL = "https://auth.ebay.com/oauth2/authorize"
    EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
    EBAY_API_BASE = "https://api.ebay.com"

# Scopes we need: read fulfillment data (orders) + account info.
EBAY_SCOPES = " ".join([
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
])


def is_configured() -> bool:
    return bool(EBAY_CLIENT_ID and EBAY_CLIENT_SECRET)


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def authorize_url(state: str) -> str:
    params = {
        "client_id": EBAY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": EBAY_RU_NAME or EBAY_REDIRECT_URI,
        "scope": EBAY_SCOPES,
        "state": state,
        "prompt": "login",
    }
    return f"{EBAY_AUTH_URL}?{urlencode(params)}"


def _basic_auth_header() -> str:
    raw = f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


async def exchange_code(code: str) -> dict:
    """Trade an authorization code for {access_token, refresh_token, expires_in, ...}."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            EBAY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": EBAY_RU_NAME or EBAY_REDIRECT_URI,
            },
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        r.raise_for_status()
        return r.json()


async def refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            EBAY_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": EBAY_SCOPES,
            },
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Field mapping — eBay order -> ParsedSale
# ---------------------------------------------------------------------------


def _to_float(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    # eBay ISO 8601: 2026-05-12T14:33:21.000Z
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def map_order_to_sales(order: dict) -> list[ParsedSale]:
    """Flatten one eBay Order into one ParsedSale per line item."""
    sales: list[ParsedSale] = []
    sale_date = _to_date(order.get("creationDate") or order.get("orderFulfillmentStatus", {}).get("date"))
    if not sale_date:
        sale_date = date.today()

    # eBay fee breakdowns are on the order, not the line — distribute proportionally.
    pricing = order.get("pricingSummary") or {}
    order_total = _to_float(pricing.get("priceSubtotal"))
    order_fee_total = 0.0
    order_ship_charged = _to_float(pricing.get("deliveryCost"))
    order_ship_cost = 0.0  # eBay doesn't expose seller's label cost via this endpoint

    for adj in (pricing.get("fee") or pricing.get("fees") or []):
        order_fee_total += _to_float(adj)
    # The actual fee breakdown lives under "totalMarketplaceFee" on most exports.
    if "totalMarketplaceFee" in pricing:
        order_fee_total = _to_float(pricing["totalMarketplaceFee"])
    if "totalFeeBasisAmount" in pricing and not order_fee_total:
        order_fee_total = _to_float(pricing["totalFeeBasisAmount"])

    buyer_state = ""
    ship_to = (order.get("fulfillmentStartInstructions") or [{}])[0].get(
        "shippingStep", {}
    ).get("shipTo", {}).get("contactAddress", {})
    if ship_to:
        buyer_state = ship_to.get("stateOrProvince") or ""

    line_items = order.get("lineItems") or []
    if not line_items:
        return sales

    # Weight per-line for fee allocation
    line_subtotals = [_to_float((li.get("total") or {}).get("value", li.get("total"))) for li in line_items]
    total_sub = sum(line_subtotals) or order_total or 1.0

    for li, li_sub in zip(line_items, line_subtotals):
        weight = (li_sub / total_sub) if total_sub else (1.0 / len(line_items))
        line_fees = round(order_fee_total * weight, 2)
        line_ship_charged = round(order_ship_charged * weight, 2)
        line_ship_cost = round(order_ship_cost * weight, 2)

        title = li.get("title") or li.get("lineItemFulfillmentStatus") or "eBay sale"
        sale_price = li_sub
        notes_parts = []
        if buyer_state:
            notes_parts.append(f"Buyer state: {buyer_state}")
        order_id = order.get("orderId") or ""
        if order_id:
            notes_parts.append(f"eBay order {order_id}")

        sales.append(ParsedSale(
            item_title=title,
            sale_date=sale_date,
            sale_price=round(sale_price, 2),
            platform_fees=line_fees,
            shipping_charged=line_ship_charged,
            shipping_cost_out=line_ship_cost,
            platform_source="ebay",
            notes=" · ".join(notes_parts),
            raw={
                "order_id": order_id,
                "line_item_id": li.get("lineItemId"),
                "buyer_state": buyer_state,
            },
        ))
    return sales


# ---------------------------------------------------------------------------
# Sell Fulfillment API — list orders
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    sales: list[ParsedSale]
    raw_orders: int
    next_offset: Optional[int] = None


async def fetch_orders(
    access_token: str,
    since: Optional[date] = None,
    limit: int = 200,
) -> FetchResult:
    """Pull recent paid orders. eBay's filter syntax uses ISO 8601 UTC."""
    if since is None:
        since = date.today() - timedelta(days=90)
    since_iso = datetime.combine(since, datetime.min.time()).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    # orderfulfillmentstatus filter pulls fulfilled or partially-fulfilled orders.
    filter_expr = (
        f"creationdate:[{since_iso}..],"
        "orderfulfillmentstatus:{FULFILLED|IN_PROGRESS}"
    )
    params = {"filter": filter_expr, "limit": str(min(limit, 200))}
    url = f"{EBAY_API_BASE}/sell/fulfillment/v1/order?{urlencode(params)}"

    all_sales: list[ParsedSale] = []
    raw_count = 0
    async with httpx.AsyncClient(timeout=30) as client:
        next_url: Optional[str] = url
        pages = 0
        while next_url and pages < 10:  # cap at 10 pages per sync (~2000 orders)
            r = await client.get(
                next_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                },
            )
            if r.status_code == 401:
                raise PermissionError("ebay_token_expired")
            r.raise_for_status()
            data = r.json()
            orders = data.get("orders") or []
            raw_count += len(orders)
            for o in orders:
                all_sales.extend(map_order_to_sales(o))
            next_url = data.get("next")
            pages += 1

    return FetchResult(sales=all_sales, raw_orders=raw_count)
