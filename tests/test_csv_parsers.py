"""Tests for backend/csv_parsers.py — per-platform parsing + detection + edge cases."""

from __future__ import annotations

from datetime import date

import pytest

from backend.csv_parsers import (
    _money,
    _parse_date,
    csv_headers,
    detect_platform,
    parse_comc,
    parse_csv,
    parse_ebay,
    parse_generic,
    parse_mercari,
    parse_myslabs,
    parse_tcgplayer,
    parse_whatnot,
)


# ---------------------------------------------------------------------------
# Money / date helpers
# ---------------------------------------------------------------------------


class TestMoneyParsing:
    def test_simple_dollar_amount(self):
        assert _money("$1,234.56") == pytest.approx(1234.56)

    def test_negative_in_parens(self):
        assert _money("($100.00)") == pytest.approx(-100.0)

    def test_negative_with_minus(self):
        assert _money("-12.34") == pytest.approx(-12.34)

    def test_blank_returns_zero(self):
        assert _money("") == 0.0
        assert _money(None) == 0.0
        assert _money("N/A") == 0.0

    def test_invalid_returns_zero(self):
        assert _money("garbage") == 0.0


class TestDateParsing:
    def test_iso_format(self):
        assert _parse_date("2025-06-15") == date(2025, 6, 15)

    def test_us_slash_format(self):
        assert _parse_date("06/15/2025") == date(2025, 6, 15)

    def test_long_month_name(self):
        assert _parse_date("June 15, 2025") == date(2025, 6, 15)

    def test_short_month_name(self):
        assert _parse_date("Jun 15, 2025") == date(2025, 6, 15)

    def test_iso_with_time(self):
        assert _parse_date("2025-06-15T12:30:00") == date(2025, 6, 15)

    def test_invalid_returns_none(self):
        assert _parse_date("not a date") is None
        assert _parse_date("") is None
        assert _parse_date(None) is None


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


class TestPlatformDetection:
    def test_detect_ebay(self):
        headers = ["Item title", "Sold for", "Transaction date", "Total fees"]
        assert detect_platform(headers) == "ebay"

    def test_detect_whatnot(self):
        headers = ["Lot Title", "Sale Price", "Whatnot Fee", "Show Name"]
        assert detect_platform(headers) == "whatnot"

    def test_detect_comc(self):
        headers = ["Description", "Sale Price", "COMC Fees", "Date Sold"]
        assert detect_platform(headers) == "comc"

    def test_detect_myslabs(self):
        headers = ["Title", "MySlabs Fee", "Sold Price", "Slab"]
        assert detect_platform(headers) == "myslabs"

    def test_detect_tcgplayer(self):
        headers = ["Order Number", "Product Name", "Item Total", "TCGplayer Fee"]
        assert detect_platform(headers) == "tcgplayer"

    def test_detect_mercari(self):
        headers = ["Item ID", "Item Name", "Sold Price", "Mercari Fee"]
        assert detect_platform(headers) == "mercari"

    def test_detect_generic_fallback(self):
        headers = ["Foo", "Bar", "Baz"]
        assert detect_platform(headers) == "generic"


# ---------------------------------------------------------------------------
# eBay parser
# ---------------------------------------------------------------------------


EBAY_CSV = """Some preamble row that eBay adds
,,,
Item title,Transaction date,Sold for,Total fees,Shipping and handling,Shipping label cost
2021 Topps Chrome Mike Trout #1,2025-06-15,$120.00,$15.60,$5.00,$4.50
1986 Fleer Michael Jordan #57 PSA 8,2025-07-20,"$1,500.00","$195.00",$0.00,$8.75
"""


class TestEbayParser:
    def test_parses_two_rows(self):
        sales = parse_ebay(EBAY_CSV)
        assert len(sales) == 2

    def test_skips_preamble(self):
        sales = parse_ebay(EBAY_CSV)
        assert sales[0].item_title == "2021 Topps Chrome Mike Trout #1"

    def test_parses_dollar_amounts(self):
        sales = parse_ebay(EBAY_CSV)
        assert sales[1].sale_price == pytest.approx(1_500.0)
        assert sales[1].platform_fees == pytest.approx(195.0)

    def test_parses_dates(self):
        sales = parse_ebay(EBAY_CSV)
        assert sales[0].sale_date == date(2025, 6, 15)

    def test_platform_source(self):
        sales = parse_ebay(EBAY_CSV)
        assert all(s.platform_source == "ebay" for s in sales)


# ---------------------------------------------------------------------------
# Whatnot parser
# ---------------------------------------------------------------------------


WHATNOT_CSV = """Lot Title,Sale Date,Sale Price,Whatnot Fee,Shipping Paid,Shipping Cost
Pokemon Charizard Holo,2025-06-15,$250.00,$25.00,$5.00,$4.00
NBA Hoops Pack,2025-06-16,$50.00,$5.00,$5.00,$4.00
"""


class TestWhatnotParser:
    def test_parses_rows(self):
        sales = parse_whatnot(WHATNOT_CSV)
        assert len(sales) == 2

    def test_extracts_fees(self):
        sales = parse_whatnot(WHATNOT_CSV)
        assert sales[0].platform_fees == pytest.approx(25.0)

    def test_extracts_shipping(self):
        sales = parse_whatnot(WHATNOT_CSV)
        assert sales[0].shipping_charged == pytest.approx(5.0)
        assert sales[0].shipping_cost_out == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# COMC parser
# ---------------------------------------------------------------------------


COMC_CSV = """Description,Date Sold,Sale Price,Commission,Shipping Cost
2020 Topps Update Luis Robert RC,2025-05-01,$45.00,$9.00,$2.00
1989 Upper Deck Ken Griffey Jr,2025-05-15,$120.00,$24.00,$3.00
"""


class TestComcParser:
    def test_parses_consignment(self):
        sales = parse_comc(COMC_CSV)
        assert len(sales) == 2
        assert sales[0].platform_source == "comc"

    def test_no_shipping_charged_on_consignment(self):
        # COMC handles shipping on consignment; user pays no shipping
        sales = parse_comc(COMC_CSV)
        assert sales[0].shipping_charged == 0.0


# ---------------------------------------------------------------------------
# MySlabs parser
# ---------------------------------------------------------------------------


MYSLABS_CSV = """Title,Sale Date,Sold Price,MySlabs Fee,Shipping Cost
2018 Bowman Chrome Acuna PSA 10,2025-04-01,$300.00,$30.00,$10.00
"""


class TestMySlabsParser:
    def test_parses_slab(self):
        sales = parse_myslabs(MYSLABS_CSV)
        assert len(sales) == 1
        assert sales[0].sale_price == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# TCGPlayer parser
# ---------------------------------------------------------------------------


TCG_CSV = """Order Number,Product Name,Order Date,Item Total,Commission Fee,Shipping Charged
100123,Black Lotus,2025-03-01,$5000.00,$500.00,$10.00
"""


class TestTCGPlayerParser:
    def test_parses_tcg(self):
        sales = parse_tcgplayer(TCG_CSV)
        assert len(sales) == 1
        assert sales[0].sale_price == pytest.approx(5_000.0)


# ---------------------------------------------------------------------------
# Mercari parser
# ---------------------------------------------------------------------------


MERCARI_CSV = """Item ID,Item Name,Sold Date,Sold Price,Mercari Fee
ABC123,2023 Donruss Bryce Harper,2025-02-15,$80.00,$8.00
"""


class TestMercariParser:
    def test_parses_mercari(self):
        sales = parse_mercari(MERCARI_CSV)
        assert len(sales) == 1
        assert sales[0].platform_source == "mercari"


# ---------------------------------------------------------------------------
# Generic CSV with caller mapping
# ---------------------------------------------------------------------------


class TestGenericParser:
    def test_with_mapping(self):
        text = "My Title Col,My Date Col,My Price Col\nSome card,2025-06-15,$100\n"
        mapping = {
            "item_title": "My Title Col",
            "sale_date": "My Date Col",
            "sale_price": "My Price Col",
        }
        sales = parse_generic(text, mapping)
        assert len(sales) == 1
        assert sales[0].item_title == "Some card"
        assert sales[0].sale_price == pytest.approx(100.0)

    def test_missing_required_mapping_returns_empty(self):
        text = "a,b,c\n1,2,3\n"
        # Missing required mapping fields
        sales = parse_generic(text, {})
        assert sales == []


# ---------------------------------------------------------------------------
# Top-level parse_csv dispatch
# ---------------------------------------------------------------------------


class TestParseCSVDispatch:
    def test_dispatches_to_ebay(self):
        sales, detected = parse_csv(EBAY_CSV)
        assert detected == "ebay"
        assert len(sales) == 2

    def test_dispatches_to_whatnot(self):
        sales, detected = parse_csv(WHATNOT_CSV)
        assert detected == "whatnot"

    def test_explicit_hint_overrides(self):
        sales, detected = parse_csv(WHATNOT_CSV, platform_hint="whatnot")
        assert detected == "whatnot"

    def test_generic_without_mapping_returns_empty(self):
        text = "Foo,Bar\n1,2\n"
        sales, detected = parse_csv(text)
        assert detected == "generic"
        assert sales == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_csv(self):
        sales, detected = parse_csv("")
        assert sales == []

    def test_only_headers_no_data(self):
        headers_only = "Item title,Transaction date,Sold for\n"
        sales = parse_ebay(headers_only)
        assert sales == []

    def test_empty_rows_skipped(self):
        csv = (
            "Item title,Transaction date,Sold for,Total fees\n"
            ",,,\n"
            "Card A,2025-06-01,$100,$10\n"
            "\n"
            "Card B,2025-06-02,$200,$20\n"
        )
        sales = parse_ebay(csv)
        # Should skip the all-empty row
        assert len(sales) == 2

    def test_missing_title_skipped(self):
        csv = (
            "Item title,Transaction date,Sold for\n"
            ",2025-06-01,$100\n"
            "Card A,2025-06-02,$200\n"
        )
        sales = parse_ebay(csv)
        # row with no title is dropped
        assert len(sales) == 1
        assert sales[0].item_title == "Card A"

    def test_missing_date_skipped(self):
        csv = (
            "Item title,Transaction date,Sold for\n"
            "Card A,,$100\n"
            "Card B,2025-06-02,$200\n"
        )
        sales = parse_ebay(csv)
        assert len(sales) == 1
        assert sales[0].item_title == "Card B"

    def test_zero_dollar_amount(self):
        csv = (
            "Item title,Transaction date,Sold for,Total fees\n"
            "Card,2025-06-01,$0.00,$0\n"
        )
        sales = parse_ebay(csv)
        assert len(sales) == 1
        assert sales[0].sale_price == 0.0

    def test_negative_amount_as_return(self):
        csv = (
            "Item title,Transaction date,Sold for,Total fees\n"
            "Refund,2025-06-01,($50.00),$0\n"
        )
        sales = parse_ebay(csv)
        assert sales[0].sale_price == pytest.approx(-50.0)

    def test_unicode_in_card_name(self):
        csv = (
            "Item title,Transaction date,Sold for,Total fees\n"
            "Pokémon Pikachu ⚡ Holo,2025-06-01,$100,$10\n"
        )
        sales = parse_ebay(csv)
        assert sales[0].item_title == "Pokémon Pikachu ⚡ Holo"

    def test_quotes_in_card_name(self):
        # CSV with quoted title containing comma
        csv = (
            'Item title,Transaction date,Sold for,Total fees\n'
            '"Mantle, Mickey 1952 Topps",2025-06-01,$100,$10\n'
        )
        sales = parse_ebay(csv)
        assert sales[0].item_title == "Mantle, Mickey 1952 Topps"

    def test_csv_headers_function(self):
        text = "\n\nCol A,Col B,Col C\nfoo,bar,baz\n"
        headers = csv_headers(text)
        assert headers == ["Col A", "Col B", "Col C"]
