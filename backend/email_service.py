"""Email delivery — SendGrid HTTP API with graceful console fallback.

Templates live in this module as inline functions that return (subject, html, text).
The HTML matches the CardTax brand: cream paper background, deep ink text,
Newsreader serif headings via a Google Fonts <link>, JetBrains Mono for numbers.

Public surface:
    send_welcome(to, display_name)
    send_password_reset(to, reset_url)
    send_subscription_confirmation(to, tier_name, price_display)
    send_subscription_cancellation(to, tier_name)
    send_tax_deadline_reminder(to, deadline_label, due_date_iso, estimated_amount)
    send_weekly_digest(to, summary)   # summary is a small dict; see template
    send(to, subject, html, text)     # low-level escape hatch
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Optional

import httpx

log = logging.getLogger("cardtax.email")

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
EMAIL_FROM = os.environ.get("CARDTAX_EMAIL_FROM", "noreply@cardtax.app")
EMAIL_FROM_NAME = os.environ.get("CARDTAX_EMAIL_FROM_NAME", "CardTax")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")

SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"


def is_configured() -> bool:
    return bool(SENDGRID_API_KEY)


# ---------------------------------------------------------------------------
# Brand-matched HTML wrapper
# ---------------------------------------------------------------------------

_BASE_STYLES = """
  body { margin:0; padding:0; background:#f3efe5; font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',Arial,sans-serif; color:#1a1a17; }
  .wrap { max-width:560px; margin:0 auto; padding:32px 20px; }
  .card { background:#faf8f3; border:1px solid #e3ddcd; border-radius:2px; padding:36px 32px; }
  .brand { font-family:Georgia,'Times New Roman',serif; font-size:22px; font-weight:500; letter-spacing:-0.02em; color:#1a1a17; margin-bottom:24px; }
  .brand .dot { display:inline-block; width:5px; height:5px; border-radius:50%; background:#1e3a5f; margin-left:4px; vertical-align:middle; }
  h1 { font-family:Georgia,'Times New Roman',serif; font-size:26px; font-weight:500; letter-spacing:-0.02em; color:#1a1a17; margin:0 0 18px; line-height:1.15; }
  p { font-size:15px; line-height:1.6; color:#1a1a17; margin:0 0 16px; }
  .muted { color:#80796b; font-size:13px; }
  .num { font-family:'JetBrains Mono','SF Mono',Menlo,monospace; font-feature-settings:'tnum','zero'; color:#1a1a17; }
  .btn { display:inline-block; background:#1a1a17; color:#faf8f3 !important; text-decoration:none; padding:11px 22px; font-size:14px; font-weight:500; border-radius:2px; }
  .panel { background:#f3efe5; border:1px solid #e3ddcd; border-radius:2px; padding:18px 20px; margin:18px 0; }
  .row { display:flex; justify-content:space-between; padding:8px 0; border-bottom:1px solid #e3ddcd; font-size:14px; }
  .row:last-child { border-bottom:none; }
  .footer { text-align:center; margin-top:24px; color:#80796b; font-size:12px; line-height:1.5; }
  .footer a { color:#80796b; }
  a { color:#1e3a5f; }
"""


def _wrap(inner_html: str, preheader: str = "") -> str:
    preheader_block = (
        f'<div style="display:none;max-height:0;overflow:hidden;color:transparent">{preheader}</div>'
        if preheader else ""
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CardTax</title>
<style>{_BASE_STYLES}</style>
</head><body>
{preheader_block}
<div class="wrap">
  <div class="brand">CardTax<span class="dot"></span></div>
  <div class="card">
    {inner_html}
  </div>
  <div class="footer">
    CardTax — tax tracking for card sellers.<br>
    <a href="{APP_BASE_URL}/account#email-prefs">Email preferences</a> · <a href="{APP_BASE_URL}">Open the app</a>
  </div>
</div>
</body></html>"""


def _money(n: float) -> str:
    if n is None:
        return "$0"
    neg = n < 0
    abs_n = abs(n)
    formatted = f"${abs_n:,.2f}"
    return ("-" + formatted) if neg else formatted


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _tpl_welcome(display_name: str) -> tuple[str, str, str]:
    name = display_name or "there"
    subject = "Welcome to CardTax"
    html = _wrap(f"""
      <h1>Welcome, {name}.</h1>
      <p>Thanks for signing up. CardTax turns your card sales — from eBay, Whatnot, COMC, or anywhere
      else — into IRS-ready tax estimates without the spreadsheet sprawl.</p>
      <p>Three things to get you started:</p>
      <div class="panel">
        <p style="margin:0 0 8px"><strong>1.</strong> <a href="{APP_BASE_URL}/upload">Import a CSV</a> from eBay or Whatnot.</p>
        <p style="margin:0 0 8px"><strong>2.</strong> <a href="{APP_BASE_URL}/settings">Set your state and filing status</a> so local tax is included.</p>
        <p style="margin:0"><strong>3.</strong> <a href="{APP_BASE_URL}/quiz">Take the 9-factor quiz</a> to see if you're a hobbyist, investor, or dealer.</p>
      </div>
      <p style="margin-top:24px"><a href="{APP_BASE_URL}/dashboard" class="btn">Open the dashboard</a></p>
      <p class="muted" style="margin-top:28px">Questions? Just hit the chat bubble in the corner of the app — we read every message.</p>
    """, preheader="Get your card sales into IRS-ready shape.")
    text = (
        f"Welcome, {name}.\n\n"
        "Thanks for signing up to CardTax. Three things to start:\n"
        f"1. Import a CSV: {APP_BASE_URL}/upload\n"
        f"2. Set your state and filing status: {APP_BASE_URL}/settings\n"
        f"3. Take the classification quiz: {APP_BASE_URL}/quiz\n\n"
        f"Open the dashboard: {APP_BASE_URL}/dashboard\n"
    )
    return subject, html, text


def _tpl_email_verification(verify_url: str) -> tuple[str, str, str]:
    subject = "Verify your CardTax email"
    html = _wrap(f"""
      <h1>Confirm your email</h1>
      <p>Welcome to CardTax. Please confirm this is your email address so we can send you tax-deadline reminders and receipts. The link is good for 24 hours.</p>
      <p style="margin:24px 0"><a href="{verify_url}" class="btn">Verify email</a></p>
      <p class="muted">Or paste this into your browser:<br>
      <span class="num" style="word-break:break-all">{verify_url}</span></p>
      <p class="muted" style="margin-top:24px">If you didn't sign up for CardTax, you can safely ignore this email.</p>
    """, preheader="Verify your CardTax email — link valid for 24 hours.")
    text = (
        "Confirm your CardTax email.\n\n"
        f"Click to verify (link valid for 24 hours):\n{verify_url}\n\n"
        "If you didn't sign up for CardTax, you can safely ignore this email.\n"
    )
    return subject, html, text


def _tpl_password_reset(reset_url: str) -> tuple[str, str, str]:
    subject = "Reset your CardTax password"
    html = _wrap(f"""
      <h1>Reset your password</h1>
      <p>We got a request to reset the password for your CardTax account. Click the link below to choose a new one. The link is good for one hour.</p>
      <p style="margin:24px 0"><a href="{reset_url}" class="btn">Reset password</a></p>
      <p class="muted">Or paste this into your browser:<br>
      <span class="num" style="word-break:break-all">{reset_url}</span></p>
      <p class="muted" style="margin-top:24px">If you didn't request this, you can safely ignore this email — your password won't change.</p>
    """, preheader="Reset link valid for one hour.")
    text = (
        "Reset your CardTax password.\n\n"
        f"Click to choose a new password (link valid for 1 hour):\n{reset_url}\n\n"
        "If you didn't request this, you can safely ignore this email.\n"
    )
    return subject, html, text


def _tpl_subscription_confirmation(tier_name: str, price_display: str) -> tuple[str, str, str]:
    subject = f"You're on CardTax {tier_name}"
    html = _wrap(f"""
      <h1>Subscription confirmed.</h1>
      <p>You're now on the <strong>{tier_name}</strong> plan ({price_display}/month). Thanks for supporting CardTax.</p>
      <div class="panel">
        <div class="row"><span class="muted">Plan</span><span class="num">{tier_name}</span></div>
        <div class="row"><span class="muted">Price</span><span class="num">{price_display} / month</span></div>
      </div>
      <p>Manage your plan, view invoices, or cancel any time from your account page.</p>
      <p style="margin-top:24px"><a href="{APP_BASE_URL}/account" class="btn">Manage billing</a></p>
    """, preheader=f"You're now on CardTax {tier_name}.")
    text = (
        f"Subscription confirmed — you're on the {tier_name} plan ({price_display}/month).\n\n"
        f"Manage your plan: {APP_BASE_URL}/account\n"
    )
    return subject, html, text


def _tpl_subscription_cancellation(tier_name: str) -> tuple[str, str, str]:
    subject = "Your CardTax subscription was cancelled"
    html = _wrap(f"""
      <h1>Subscription cancelled.</h1>
      <p>Your <strong>{tier_name}</strong> subscription has been cancelled. You'll keep access to paid features until
      the end of your current billing period, then drop to the Free tier.</p>
      <p>Your data stays put — every transaction, every setting. Come back any time.</p>
      <p style="margin-top:24px"><a href="{APP_BASE_URL}/pricing" class="btn">View plans</a></p>
      <p class="muted" style="margin-top:24px">If this was a mistake, you can resubscribe from your account page.</p>
    """, preheader="You'll keep paid features until the end of the billing period.")
    text = (
        f"Your CardTax {tier_name} subscription was cancelled.\n\n"
        "You'll keep access to paid features until the end of your billing period.\n"
        f"Resubscribe any time: {APP_BASE_URL}/pricing\n"
    )
    return subject, html, text


def _tpl_tax_deadline(deadline_label: str, due_date_iso: str, estimated_amount: Optional[float]) -> tuple[str, str, str]:
    amt_line = ""
    if estimated_amount is not None:
        amt_line = f"""
        <div class="panel">
          <div class="row"><span class="muted">Estimated payment</span><span class="num">{_money(estimated_amount)}</span></div>
          <div class="row"><span class="muted">Due</span><span class="num">{due_date_iso}</span></div>
        </div>
        """
    subject = f"Reminder: {deadline_label} estimated tax is due {due_date_iso}"
    html = _wrap(f"""
      <h1>{deadline_label} estimated tax.</h1>
      <p>The IRS deadline for <strong>{deadline_label}</strong> quarterly estimated tax is <strong>{due_date_iso}</strong>.
      If your card sales are pushing you past safe-harbor thresholds, now's the time to pay.</p>
      {amt_line}
      <p style="margin-top:24px"><a href="{APP_BASE_URL}/tax-summary" class="btn">View tax summary</a></p>
      <p class="muted" style="margin-top:24px">CardTax estimates only. Confirm with your CPA before paying.</p>
    """, preheader=f"{deadline_label} estimated tax is due {due_date_iso}.")
    text = (
        f"{deadline_label} estimated tax is due {due_date_iso}.\n\n"
        + (f"Estimated payment: {_money(estimated_amount)}\n\n" if estimated_amount is not None else "")
        + f"View your tax summary: {APP_BASE_URL}/tax-summary\n"
    )
    return subject, html, text


def _tpl_weekly_digest(summary: dict) -> tuple[str, str, str]:
    """summary keys: imported_count, gross_sales, net_gain, last_platforms (list[str])"""
    n = int(summary.get("imported_count") or 0)
    gross = float(summary.get("gross_sales") or 0.0)
    net = float(summary.get("net_gain") or 0.0)
    plats = summary.get("last_platforms") or []
    plat_line = ", ".join(plats) if plats else "—"

    subject = f"Your CardTax week: {n} new transaction" + ("" if n == 1 else "s")
    html = _wrap(f"""
      <h1>This week in CardTax.</h1>
      <p>Here's a quick look at what landed in your account over the past 7 days.</p>
      <div class="panel">
        <div class="row"><span class="muted">New transactions</span><span class="num">{n}</span></div>
        <div class="row"><span class="muted">Gross sales</span><span class="num">{_money(gross)}</span></div>
        <div class="row"><span class="muted">Net gain</span><span class="num">{_money(net)}</span></div>
        <div class="row"><span class="muted">Sources</span><span class="num">{plat_line}</span></div>
      </div>
      <p style="margin-top:24px"><a href="{APP_BASE_URL}/dashboard" class="btn">Open dashboard</a></p>
      <p class="muted" style="margin-top:18px">You can turn off the weekly digest any time from <a href="{APP_BASE_URL}/account#email-prefs">your email preferences</a>.</p>
    """, preheader=f"{n} new transactions this week.")
    text = (
        "This week in CardTax.\n\n"
        f"New transactions: {n}\n"
        f"Gross sales: {_money(gross)}\n"
        f"Net gain: {_money(net)}\n"
        f"Sources: {plat_line}\n\n"
        f"Open dashboard: {APP_BASE_URL}/dashboard\n"
    )
    return subject, html, text


# ---------------------------------------------------------------------------
# Quarterly estimated-tax deadlines (per IRS Form 1040-ES, 2025/2026 cycle).
# January 15 covers Q4 of the *previous* year.
# ---------------------------------------------------------------------------

QUARTERLY_DEADLINES = [
    ("Q4", 1, 15),
    ("Q1", 4, 15),
    ("Q2", 6, 15),
    ("Q3", 9, 15),
]


def quarterly_label_for(due: date) -> str:
    for label, m, d in QUARTERLY_DEADLINES:
        if due.month == m and due.day == d:
            return label
    return ""


def next_deadline(today: date) -> tuple[str, date]:
    """Return the next quarterly deadline (label, date) from `today`."""
    candidates = []
    for label, m, d in QUARTERLY_DEADLINES:
        candidates.append((label, date(today.year, m, d)))
        candidates.append((label, date(today.year + 1, m, d)))
    candidates = [c for c in candidates if c[1] >= today]
    candidates.sort(key=lambda c: c[1])
    return candidates[0]


# ---------------------------------------------------------------------------
# Low-level send
# ---------------------------------------------------------------------------


def send(to: str, subject: str, html: str, text: str = "") -> bool:
    """Send an email. Returns True on success.

    If SENDGRID_API_KEY is not set, logs to console and returns True (treated as
    success so the rest of the flow proceeds normally during dev).
    """
    if not to or "@" not in to:
        log.warning("email skipped — invalid 'to' address: %r", to)
        return False

    if not is_configured():
        # Make this visible even with default Python logging (WARN+).
        print(
            f"[email:console-fallback] to={to} subject={subject!r}\n"
            f"--- text ---\n{text or '(no text part)'}\n--- end ---",
            flush=True,
        )
        return True

    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": EMAIL_FROM, "name": EMAIL_FROM_NAME},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text or _strip_html(html)},
            {"type": "text/html", "value": html},
        ],
    }
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(
                SENDGRID_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {SENDGRID_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
        if r.status_code >= 400:
            log.error("SendGrid error %s sending to %s: %s", r.status_code, to, r.text[:300])
            return False
        return True
    except Exception as e:
        log.exception("SendGrid request failed sending to %s: %s", to, e)
        return False


def _strip_html(html: str) -> str:
    import re as _re
    text = _re.sub(r"<style[^>]*>.*?</style>", "", html, flags=_re.DOTALL | _re.IGNORECASE)
    text = _re.sub(r"<[^>]+>", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Public typed senders
# ---------------------------------------------------------------------------


def send_welcome(to: str, display_name: str = "") -> bool:
    subject, html, text = _tpl_welcome(display_name)
    return send(to, subject, html, text)


def send_password_reset(to: str, reset_url: str) -> bool:
    subject, html, text = _tpl_password_reset(reset_url)
    return send(to, subject, html, text)


def send_email_verification(to: str, verify_url: str) -> bool:
    subject, html, text = _tpl_email_verification(verify_url)
    return send(to, subject, html, text)


def send_subscription_confirmation(to: str, tier_name: str, price_display: str) -> bool:
    subject, html, text = _tpl_subscription_confirmation(tier_name, price_display)
    return send(to, subject, html, text)


def send_subscription_cancellation(to: str, tier_name: str) -> bool:
    subject, html, text = _tpl_subscription_cancellation(tier_name)
    return send(to, subject, html, text)


def send_tax_deadline_reminder(
    to: str, deadline_label: str, due_date_iso: str, estimated_amount: Optional[float] = None
) -> bool:
    subject, html, text = _tpl_tax_deadline(deadline_label, due_date_iso, estimated_amount)
    return send(to, subject, html, text)


def send_weekly_digest(to: str, summary: dict) -> bool:
    subject, html, text = _tpl_weekly_digest(summary)
    return send(to, subject, html, text)
