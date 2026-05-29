"""PDF tax report generation.

Builds four reports for download from the Tax Summary page:
  - Form 8949 (Parts I & II, Boxes A-F)
  - Schedule D (Parts I, II, III)
  - Schedule C (dealers)
  - Tax Summary — comprehensive federal + state + local breakdown

Uses ReportLab Platypus for layout. Typography mirrors the in-app design:
serif headings (Times-Roman as a stand-in for Newsreader), mono numbers,
hairline rules, sober ink palette.
"""

from __future__ import annotations

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)


# ---------------------------------------------------------------------------
# Shared palette + styles — mirrors the app's editorial design
# ---------------------------------------------------------------------------

INK         = colors.HexColor("#1a1a17")
INK_SOFT    = colors.HexColor("#4a4944")
INK_MUTE    = colors.HexColor("#80796b")
RULE        = colors.HexColor("#cfc7b2")
RULE_FAINT  = colors.HexColor("#e3ddcd")
PAPER       = colors.HexColor("#faf8f3")
PAPER_ALT   = colors.HexColor("#f3efe5")
ACCENT      = colors.HexColor("#1e3a5f")
GAIN        = colors.HexColor("#1f5d3a")
LOSS        = colors.HexColor("#8b2a2a")


def _styles():
    ss = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "Title", parent=ss["Title"],
            fontName="Times-Roman", fontSize=22, leading=26,
            textColor=INK, spaceAfter=2, alignment=TA_LEFT,
        ),
        "subtitle": ParagraphStyle(
            "Subtitle", parent=ss["Normal"],
            fontName="Helvetica", fontSize=10, leading=14,
            textColor=INK_MUTE, spaceAfter=10,
        ),
        "h2": ParagraphStyle(
            "H2", parent=ss["Heading2"],
            fontName="Times-Roman", fontSize=14, leading=18,
            textColor=INK, spaceBefore=14, spaceAfter=6,
        ),
        "h3": ParagraphStyle(
            "H3", parent=ss["Heading3"],
            fontName="Times-Roman", fontSize=11, leading=14,
            textColor=INK, spaceBefore=10, spaceAfter=4,
        ),
        "eyebrow": ParagraphStyle(
            "Eyebrow", parent=ss["Normal"],
            fontName="Helvetica-Bold", fontSize=7.5, leading=10,
            textColor=INK_MUTE, spaceAfter=4, spaceBefore=4,
        ),
        "body": ParagraphStyle(
            "Body", parent=ss["Normal"],
            fontName="Helvetica", fontSize=9.5, leading=13,
            textColor=INK_SOFT, spaceAfter=4,
        ),
        "small": ParagraphStyle(
            "Small", parent=ss["Normal"],
            fontName="Helvetica", fontSize=8, leading=11,
            textColor=INK_MUTE,
        ),
        "footer": ParagraphStyle(
            "Footer", parent=ss["Normal"],
            fontName="Helvetica", fontSize=7.5, leading=10,
            textColor=INK_MUTE, alignment=TA_CENTER,
        ),
    }


STYLES = _styles()


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _money(n) -> str:
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if n < 0:
        return f"(${abs(n):,.2f})"
    return f"${n:,.2f}"


def _money0(n) -> str:
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if n < 0:
        return f"(${abs(n):,.0f})"
    return f"${n:,.0f}"


def _pct(n) -> str:
    if n is None:
        return "—"
    try:
        return f"{float(n) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _filing_label(s: str) -> str:
    return {"single": "Single", "mfj": "Married filing jointly",
            "mfs": "Married filing separately", "hoh": "Head of household"}.get(s, s or "")


def _classification_label(s: str) -> str:
    return {"hobby": "Hobby", "investor": "Investor",
            "dealer": "Dealer / Business"}.get(s, s or "")


# ---------------------------------------------------------------------------
# Document scaffolding
# ---------------------------------------------------------------------------


def _on_page(canvas, doc):
    canvas.saveState()
    # Hairline at the top
    canvas.setStrokeColor(RULE_FAINT)
    canvas.setLineWidth(0.4)
    canvas.line(doc.leftMargin, LETTER[1] - 0.45 * inch,
                LETTER[0] - doc.rightMargin, LETTER[1] - 0.45 * inch)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(INK_MUTE)
    canvas.drawString(doc.leftMargin, LETTER[1] - 0.32 * inch, "CARDTAX")
    canvas.drawRightString(
        LETTER[0] - doc.rightMargin, LETTER[1] - 0.32 * inch,
        datetime.now().strftime("Generated %Y-%m-%d"),
    )
    # Page number
    canvas.drawCentredString(
        LETTER[0] / 2.0, 0.4 * inch,
        f"Page {doc.page}",
    )
    canvas.line(doc.leftMargin, 0.55 * inch,
                LETTER[0] - doc.rightMargin, 0.55 * inch)
    canvas.restoreState()


def _new_doc(buf, title: str) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.55 * inch, rightMargin=0.55 * inch,
        topMargin=0.65 * inch, bottomMargin=0.7 * inch,
        title=title, author="CardTax",
    )


def _header_block(report_name: str, settings: dict, classification: str | None = None) -> list:
    year = settings.get("tax_year", "")
    fs = _filing_label(settings.get("filing_status", ""))
    state = settings.get("state") or "—"
    city = (settings.get("city") or "").split(":")[-1] if settings.get("city") else ""
    cls_line = (
        f"Classification: {_classification_label(classification)}"
        if classification else ""
    )
    parts = [
        Paragraph(report_name, STYLES["title"]),
        Paragraph(
            f"Tax year {year} &nbsp;·&nbsp; {fs} &nbsp;·&nbsp; "
            f"{state}{' — ' + city if city else ''}"
            + (f" &nbsp;·&nbsp; {cls_line}" if cls_line else ""),
            STYLES["subtitle"],
        ),
    ]
    return parts


def _kv_table(rows: list[tuple[str, str]], col_widths=None, total_row: bool = False) -> Table:
    cw = col_widths or [3.6 * inch, 2.4 * inch]
    t = Table(rows, colWidths=cw)
    style = [
        ("FONTNAME", (0, 0), (0, -1), "Helvetica"),
        ("FONTNAME", (1, 0), (1, -1), "Courier"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), INK_SOFT),
        ("TEXTCOLOR", (1, 0), (1, -1), INK),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE_FAINT),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if total_row:
        style += [
            ("FONTNAME", (0, -1), (0, -1), "Helvetica-Bold"),
            ("FONTNAME", (1, -1), (1, -1), "Courier-Bold"),
            ("TEXTCOLOR", (0, -1), (-1, -1), INK),
            ("LINEABOVE", (0, -1), (-1, -1), 0.75, INK),
            ("LINEBELOW", (0, -1), (-1, -1), 0, colors.transparent),
            ("TOPPADDING", (0, -1), (-1, -1), 7),
        ]
    t.setStyle(TableStyle(style))
    return t


def _txn_table(rows: list[dict], include_code: bool = False) -> Table:
    """Render a table of 8949/Sch-D-style transaction rows."""
    head = [
        "Description", "Date acquired", "Date sold",
        "Proceeds", "Cost basis",
    ]
    if include_code:
        head += ["Code", "Adjust."]
    head += ["Gain / loss"]

    data = [head]
    sum_proc = 0.0
    sum_basis = 0.0
    sum_adj = 0.0
    sum_gl = 0.0
    for r in rows:
        proceeds = r.get("proceeds") or 0.0
        basis = r.get("basis") or 0.0
        adj = r.get("adjustment") or 0.0
        gl = r.get("gain_loss") or 0.0
        sum_proc += proceeds; sum_basis += basis; sum_adj += adj; sum_gl += gl
        line = [
            (r.get("description") or "")[:42],
            r.get("date_acquired") or "",
            r.get("date_sold") or "",
            _money(proceeds), _money(basis),
        ]
        if include_code:
            line += [r.get("code") or "", _money(adj)]
        line += [_money(gl)]
        data.append(line)
    total = ["Totals", "", "", _money(sum_proc), _money(sum_basis)]
    if include_code:
        total += ["", _money(sum_adj)]
    total += [_money(sum_gl)]
    data.append(total)

    if include_code:
        widths = [2.0, 0.85, 0.85, 0.85, 0.85, 0.45, 0.65, 0.95]
    else:
        widths = [2.4, 1.0, 1.0, 1.0, 1.0, 1.1]
    col_widths = [w * inch for w in widths]
    t = Table(data, colWidths=col_widths, repeatRows=1)
    style_cmds = [
        # Header
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK_MUTE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, INK),
        ("ALIGN", (3, 0), (-1, 0), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 4),

        # Body
        ("FONTNAME", (0, 1), (2, -2), "Helvetica"),
        ("FONTNAME", (3, 1), (-1, -2), "Courier"),
        ("FONTSIZE", (0, 1), (-1, -1), 8.5),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, RULE_FAINT),
        ("TOPPADDING", (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4),

        # Totals
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTNAME", (3, -1), (-1, -1), "Courier-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, INK),
        ("LINEBELOW", (0, -1), (-1, -1), 0, colors.transparent),
        ("TEXTCOLOR", (0, -1), (-1, -1), INK),
        ("TOPPADDING", (0, -1), (-1, -1), 6),
    ]
    if include_code:
        # Center the "Code" column (only present on Form 8949 tables).
        style_cmds.append(("ALIGN", (5, 1), (5, -1), "CENTER"))
    t.setStyle(TableStyle(style_cmds))
    return t


def _empty_note(text: str = "No transactions in this section.") -> Paragraph:
    return Paragraph(f"<i>{text}</i>", STYLES["small"])


# ---------------------------------------------------------------------------
# Form 8949
# ---------------------------------------------------------------------------


BOX_LABELS = {
    "A_with_basis": ("Box A", "Short-term · 1099-B basis reported to IRS"),
    "B_no_basis":   ("Box B", "Short-term · 1099-B basis NOT reported to IRS"),
    "C_no_1099b":   ("Box C", "Short-term · no Form 1099-B received"),
    "D_with_basis": ("Box D", "Long-term · 1099-B basis reported to IRS"),
    "E_no_basis":   ("Box E", "Long-term · 1099-B basis NOT reported to IRS"),
    "F_no_1099b":   ("Box F", "Long-term · no Form 1099-B received"),
}


def build_form_8949_pdf(form_8949: dict, settings: dict) -> bytes:
    buf = io.BytesIO()
    doc = _new_doc(buf, f"Form 8949 — {settings.get('tax_year', '')}")
    story: list = []
    story += _header_block("Form 8949 — Sales and Dispositions of Capital Assets", settings)
    story.append(Paragraph(
        "Use this report to populate IRS Form 8949 for collectible card sales. "
        "Long-term collectible gains then flow to the 28% Rate Gain Worksheet (Schedule D line 18).",
        STYLES["body"],
    ))
    story.append(Spacer(1, 8))
    story.append(Paragraph(form_8949.get("note", ""), STYLES["small"]))
    story.append(Spacer(1, 14))

    def render_section(part_label: str, section_key: str, section: dict):
        story.append(Paragraph(part_label, STYLES["h2"]))
        any_rows = False
        for box_key, payload in section.items():
            rows = payload.get("rows", []) if isinstance(payload, dict) else []
            if not rows:
                continue
            any_rows = True
            box_short, box_full = BOX_LABELS.get(box_key, (box_key, ""))
            story.append(Paragraph(box_short.upper(), STYLES["eyebrow"]))
            story.append(Paragraph(box_full, STYLES["small"]))
            story.append(Spacer(1, 4))
            story.append(_txn_table(rows, include_code=True))
            story.append(Spacer(1, 10))
        if not any_rows:
            story.append(_empty_note())
            story.append(Spacer(1, 6))

    render_section(
        "Part I — Short-term (held ≤ 1 year)",
        "short_term", form_8949.get("short_term", {}),
    )
    render_section(
        "Part II — Long-term (held > 1 year, 28% collectibles rate)",
        "long_term", form_8949.get("long_term", {}),
    )

    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "<b>Adjustment codes:</b> &nbsp;<b>B</b> Basis adjustment (e.g. gift carryover) · "
        "<b>L</b> Loss not allowed (personal-use property — IRC §165(c)) · "
        "<b>W</b> Wash sale (does NOT apply to collectibles) · "
        "<b>D</b> Acc. market discount included as ordinary",
        STYLES["small"],
    ))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Generated as a reference only. Verify all figures with a CPA before filing.",
        STYLES["small"],
    ))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Schedule D
# ---------------------------------------------------------------------------


def build_schedule_d_pdf(schedule_d: dict, summary: dict, settings: dict) -> bytes:
    buf = io.BytesIO()
    doc = _new_doc(buf, f"Schedule D — {settings.get('tax_year', '')}")
    story: list = []
    story += _header_block("Schedule D — Capital Gains and Losses", settings)
    story.append(Paragraph(
        "Long-term gains from collectibles flow through the 28% Rate Gain Worksheet "
        "(Schedule D line 18) rather than the standard LTCG brackets.",
        STYLES["body"],
    ))
    story.append(Spacer(1, 14))

    # Part I — Short-term
    st = schedule_d.get("part_i_short_term", {}) or {}
    story.append(Paragraph("Part I — Short-term capital gains and losses", STYLES["h2"]))
    story.append(Paragraph(
        "Assets held one year or less. Taxed as ordinary income at your marginal rate.",
        STYLES["small"],
    ))
    story.append(Spacer(1, 6))
    if st.get("transactions"):
        story.append(_txn_table(st["transactions"], include_code=False))
    else:
        story.append(_empty_note())
    story.append(Spacer(1, 8))
    story.append(_kv_table(
        [("Line 7 · Net short-term capital gain/(loss)",
          _money(st.get("line_7_net_short_term", 0.0)))],
        total_row=True,
    ))

    # Part II — Long-term
    story.append(Spacer(1, 18))
    lt = schedule_d.get("part_ii_long_term", {}) or {}
    story.append(Paragraph("Part II — Long-term capital gains and losses", STYLES["h2"]))
    story.append(Paragraph(
        "Assets held more than one year. Collectibles capped at 28% per IRC §1(h)(4).",
        STYLES["small"],
    ))
    story.append(Spacer(1, 6))
    if lt.get("transactions"):
        story.append(_txn_table(lt["transactions"], include_code=False))
    else:
        story.append(_empty_note())
    story.append(Spacer(1, 8))
    story.append(_kv_table(
        [("Line 15 · Net long-term capital gain/(loss)",
          _money(lt.get("line_15_net_long_term", 0.0)))],
        total_row=True,
    ))

    # Part III — Summary
    story.append(Spacer(1, 18))
    story.append(Paragraph("Part III — Summary", STYLES["h2"]))
    rows = [
        ("Line 16 · Combined net capital gain/(loss)", _money(schedule_d.get("line_16_net_total", 0.0))),
        ("Line 18 · 28% rate gain (collectibles long-term)",
            _money(max(0.0, lt.get("line_15_net_long_term", 0.0)))),
        ("Estimated federal tax on collectibles (28% cap)",
            _money(summary.get("collectibles_tax", 0.0))),
        ("Estimated federal tax on short-term gain (marginal)",
            _money(max(0.0, summary.get("federal_income_tax", 0.0) - summary.get("collectibles_tax", 0.0)))),
        ("Net investment income tax (3.8%)", _money(summary.get("niit", 0.0))),
        ("Total federal capital-gain tax", _money(summary.get("federal_income_tax", 0.0) + summary.get("niit", 0.0))),
    ]
    story.append(_kv_table(rows, total_row=True))

    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "If line 16 is a loss, the deductible amount is limited to $3,000 ($1,500 MFS) per year. "
        "Excess capital losses carry forward indefinitely.",
        STYLES["small"],
    ))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Schedule C
# ---------------------------------------------------------------------------


def build_schedule_c_pdf(schedule_c: dict, se_tax: dict | None, summary: dict, settings: dict) -> bytes:
    buf = io.BytesIO()
    doc = _new_doc(buf, f"Schedule C — {settings.get('tax_year', '')}")
    story: list = []
    story += _header_block(
        "Schedule C — Profit or Loss from Business",
        settings, classification="dealer",
    )
    story.append(Paragraph(
        "For sellers classified as dealers — card inventory is held for sale in the "
        "ordinary course of a trade or business. Net profit is subject to "
        "self-employment tax (15.3%) and may qualify for the §199A QBI deduction.",
        STYLES["body"],
    ))
    story.append(Spacer(1, 14))

    sc = schedule_c
    story.append(Paragraph("Part I — Income", STYLES["h2"]))
    story.append(_kv_table([
        ("Line 1 · Gross receipts or sales",        _money(sc.get("line_1_gross_receipts", 0.0))),
        ("Line 2 · Returns and allowances",         _money(sc.get("line_2_returns_allowances", 0.0))),
        ("Line 3 · Net receipts",                   _money(sc.get("line_3_net_receipts", 0.0))),
        ("Line 4 · Cost of goods sold",             _money(sc.get("line_4_cogs", 0.0))),
        ("Line 5 · Gross profit",                   _money(sc.get("line_5_gross_profit", 0.0))),
        ("Line 7 · Gross income",                   _money(sc.get("line_7_gross_income", 0.0))),
    ]))

    story.append(Spacer(1, 14))
    story.append(Paragraph("Part II — Expenses (Lines 8–27)", STYLES["h2"]))
    exp = sc.get("expenses", {}) or {}
    story.append(_kv_table([
        ("Line 10 · Commissions and platform fees", _money(exp.get("line_10_commissions_fees", 0.0))),
        ("Line 22 · Supplies",                       _money(exp.get("line_22_supplies", 0.0))),
        ("Line 24a · Travel",                        _money(exp.get("line_24a_travel", 0.0))),
        ("Line 27 · Grading and other",              _money(exp.get("line_27_grading_other", 0.0))),
        ("Shipping and postage",                     _money(exp.get("shipping_and_postage", 0.0))),
        ("Line 28 · Total expenses",                 _money(sc.get("line_28_total_expenses", 0.0))),
    ], total_row=True))

    story.append(Spacer(1, 14))
    story.append(Paragraph("Net profit / loss", STYLES["h2"]))
    story.append(_kv_table([
        ("Line 31 · Net profit or (loss)", _money(sc.get("line_31_net_profit", 0.0))),
    ], total_row=True))

    if se_tax:
        story.append(Spacer(1, 18))
        story.append(Paragraph("Schedule SE — Self-employment tax", STYLES["h2"]))
        story.append(_kv_table([
            ("Net SE earnings",                     _money(se_tax.get("net_se_earnings", 0.0))),
            ("SE base (× 92.35%)",                  _money(se_tax.get("se_base_92_35pct", 0.0))),
            ("Social Security (12.4%, capped)",     _money(se_tax.get("social_security_tax", 0.0))),
            ("Medicare (2.9%, uncapped)",           _money(se_tax.get("medicare_tax", 0.0))),
            ("Additional Medicare (0.9%)",          _money(se_tax.get("additional_medicare_tax", 0.0))),
            ("Total SE tax",                        _money(se_tax.get("total_se_tax", 0.0))),
            ("Deductible half (Schedule 1)",        _money(se_tax.get("deductible_half_schedule_1", 0.0))),
        ], total_row=True))

    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "All COGS figures use the same lot-accounting method recorded in your settings. "
        "Verify inventory and expense categorization with a CPA before filing.",
        STYLES["small"],
    ))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Tax Summary — comprehensive report
# ---------------------------------------------------------------------------


def build_tax_summary_pdf(
    summary: dict, settings: dict,
    state_detail: dict | None = None,
    local_detail: dict | None = None,
    quarterly: dict | None = None,
    amt: dict | None = None,
    se_tax: dict | None = None,
    qbi: dict | None = None,
) -> bytes:
    buf = io.BytesIO()
    doc = _new_doc(buf, f"Tax Summary — {settings.get('tax_year', '')}")
    story: list = []

    story += _header_block(
        "Tax Summary", settings,
        classification=summary.get("classification"),
    )

    # ---- KPI grid ----
    kpi_rows = [[
        _kpi_cell("Net capital gain / loss",
                  _money0(summary.get("total_net", 0.0)),
                  f"Gross {_money0(summary.get('gross_receipts', 0.0))} – basis "
                  f"{_money0(summary.get('total_basis', 0.0))}"),
        _kpi_cell("Federal",
                  _money0(summary.get("federal_income_tax", 0.0) + summary.get("niit", 0.0)),
                  f"Collectibles {_money0(summary.get('collectibles_tax', 0.0))} · NIIT "
                  f"{_money0(summary.get('niit', 0.0))}"),
        _kpi_cell("State + local",
                  _money0(summary.get("state_tax", 0.0)
                          + ((local_detail or {}).get("total_local_tax", 0.0))),
                  settings.get("state") or "—"),
        _kpi_cell("Total estimated",
                  _money0(summary.get("estimated_total_tax", 0.0)),
                  "Federal + state + local"),
    ]]
    kpi_table = Table(kpi_rows, colWidths=[1.85 * inch] * 4)
    kpi_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEABOVE", (0, 0), (-1, 0), 0.75, INK),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
        ("LINEAFTER", (0, 0), (-2, 0), 0.25, RULE_FAINT),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    story.append(Spacer(1, 6))
    story.append(kpi_table)
    story.append(Spacer(1, 18))

    # ---- Federal breakdown ----
    story.append(Paragraph("Federal income tax", STYLES["h2"]))
    fed_rows = [
        ("Net short-term capital gain", _money(summary.get("net_short_term", 0.0))),
        ("Net long-term capital gain (collectibles)", _money(summary.get("net_long_term", 0.0))),
        ("Tax on collectibles (28% cap)", _money(summary.get("collectibles_tax", 0.0))),
        ("Tax on other ordinary income (ST / SE / hobby)",
            _money(summary.get("federal_income_tax", 0.0) - summary.get("collectibles_tax", 0.0))),
        ("Net Investment Income Tax (3.8%)", _money(summary.get("niit", 0.0))),
        ("Subtotal — federal", _money(summary.get("federal_income_tax", 0.0) + summary.get("niit", 0.0))),
    ]
    story.append(_kv_table(fed_rows, total_row=True))

    if summary.get("salt_note"):
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            f"<b>SALT cap (Schedule A):</b> {summary['salt_note']}",
            STYLES["small"],
        ))

    # ---- State + local ----
    if state_detail and state_detail.get("profile_code"):
        story.append(Spacer(1, 14))
        story.append(Paragraph(
            f"State income tax · {state_detail.get('profile_code', '')}",
            STYLES["h2"],
        ))
        story.append(_kv_table([
            ("Tax on ordinary income",            _money(state_detail.get("tax_on_ordinary", 0.0))),
            ("Tax on short-term gain",            _money(state_detail.get("tax_on_st_gain", 0.0))),
            (f"Tax on long-term gain"
             + (f" ({_pct(state_detail.get('lt_rate_used'))})"
                if state_detail.get("lt_rate_used") is not None else ""),
                _money(state_detail.get("tax_on_lt_collectible_gain", 0.0))),
            ("Subtotal — state",                  _money(summary.get("state_tax", 0.0))),
        ], total_row=True))

    if local_detail:
        story.append(Spacer(1, 14))
        story.append(Paragraph(
            f"Local tax · {local_detail.get('city', 'Local')}",
            STYLES["h2"],
        ))
        local_rows = [
            (k.replace("_", " ").title(), _money(v))
            for k, v in (local_detail.get("breakdown") or {}).items()
        ]
        local_rows.append(("Subtotal — local", _money(local_detail.get("total_local_tax", 0.0))))
        story.append(_kv_table(local_rows, total_row=True))

    # ---- AMT ----
    if amt:
        story.append(Spacer(1, 14))
        story.append(Paragraph("Alternative Minimum Tax (Form 6251)", STYLES["h2"]))
        story.append(_kv_table([
            ("AMTI",                         _money(amt.get("amti", 0.0))),
            ("Exemption used",               _money(amt.get("exemption_used", 0.0))),
            ("Tentative minimum tax",        _money(amt.get("tentative_minimum_tax", 0.0))),
            ("Regular tax for comparison",   _money(amt.get("regular_tax_for_comparison", 0.0))),
            ("AMT owed",                     _money(amt.get("amt_owed", 0.0))),
        ], total_row=True))
        if amt.get("note"):
            story.append(Spacer(1, 4))
            story.append(Paragraph(amt["note"], STYLES["small"]))

    # ---- SE tax (dealer) ----
    if se_tax:
        story.append(Spacer(1, 14))
        story.append(Paragraph("Self-employment tax (Schedule SE)", STYLES["h2"]))
        story.append(_kv_table([
            ("Net SE earnings",                  _money(se_tax.get("net_se_earnings", 0.0))),
            ("Social Security (12.4%, capped)",  _money(se_tax.get("social_security_tax", 0.0))),
            ("Medicare (2.9%, uncapped)",        _money(se_tax.get("medicare_tax", 0.0))),
            ("Additional Medicare (0.9%)",       _money(se_tax.get("additional_medicare_tax", 0.0))),
            ("Total SE tax",                     _money(se_tax.get("total_se_tax", 0.0))),
            ("Deductible half (Schedule 1)",     _money(se_tax.get("deductible_half_schedule_1", 0.0))),
        ], total_row=True))

    # ---- QBI (dealer) ----
    if qbi:
        story.append(Spacer(1, 14))
        story.append(Paragraph("§199A Qualified Business Income deduction", STYLES["h2"]))
        story.append(_kv_table([
            ("Tentative (20% × QBI)",    _money(qbi.get("tentative", 0.0))),
            ("Taxable income limit",     _money(qbi.get("taxable_income_limit", 0.0))),
            ("Allowed deduction",        _money(qbi.get("deduction", 0.0))),
        ], total_row=True))

    # ---- Quarterly ----
    if quarterly:
        story.append(Spacer(1, 14))
        story.append(Paragraph("Quarterly estimated tax (Form 1040-ES)", STYLES["h2"]))
        story.append(_kv_table([
            ("Required estimated payments", _money(quarterly.get("required_estimated_payments", 0.0))),
            ("Per quarter",                 _money(quarterly.get("per_quarter", 0.0))),
            ("Safe-harbor amount",          _money(quarterly.get("safe_harbor_amount", 0.0))),
            ("Withholding to date",         _money(quarterly.get("withholding_paid", 0.0))),
            ("Safe-harbor method",          (quarterly.get("safe_harbor_method") or "").replace("_", " ").title()),
            ("Penalty risk",                "Yes" if quarterly.get("penalty_risk") else "No"),
        ]))

        due = quarterly.get("due_dates") or []
        if due:
            story.append(Spacer(1, 6))
            head = ["Quarter", "Due date", "Amount"]
            rows_q = [head] + [
                [f"Q{i + 1}", d, _money(quarterly.get("per_quarter", 0.0))]
                for i, d in enumerate(due)
            ]
            qt = Table(rows_q, colWidths=[1.4 * inch, 2.2 * inch, 2.4 * inch])
            qt.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 7.5),
                ("TEXTCOLOR", (0, 0), (-1, 0), INK_MUTE),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, INK),
                ("FONTNAME", (0, 1), (1, -1), "Helvetica"),
                ("FONTNAME", (2, 1), (2, -1), "Courier"),
                ("FONTSIZE", (0, 1), (-1, -1), 9),
                ("ALIGN", (2, 1), (2, -1), "RIGHT"),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE_FAINT),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.append(qt)

    # ---- Notes ----
    notes = summary.get("notes") or []
    if notes:
        story.append(Spacer(1, 16))
        story.append(Paragraph("Compliance notes", STYLES["h2"]))
        for n in notes:
            story.append(Paragraph(f"• {n}", STYLES["small"]))
            story.append(Spacer(1, 2))

    story.append(Spacer(1, 18))
    story.append(Paragraph(
        "This summary is a calculation aid based on the figures you entered and the published "
        "2025 brackets / thresholds. It is not tax advice. Review with a CPA before filing.",
        STYLES["small"],
    ))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return buf.getvalue()


def _kpi_cell(label: str, value: str, sub: str) -> Table:
    inner = [
        [Paragraph(label.upper(), ParagraphStyle(
            "kpi_lbl", fontName="Helvetica-Bold", fontSize=7,
            leading=10, textColor=INK_MUTE,
        ))],
        [Paragraph(value, ParagraphStyle(
            "kpi_val", fontName="Times-Roman", fontSize=20,
            leading=22, textColor=INK,
        ))],
        [Paragraph(sub, ParagraphStyle(
            "kpi_sub", fontName="Helvetica", fontSize=7.5,
            leading=10, textColor=INK_MUTE,
        ))],
    ]
    t = Table(inner, colWidths=[1.6 * inch])
    t.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t
