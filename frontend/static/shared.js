// CardTax shell — editorial top bar, no sidebar.
// Analytics is loaded via /static/analytics.js (included separately in each
// template head). If it hasn't run yet, gtagEvent falls back to a noop.
if (typeof window.gtagEvent !== "function") {
  window.gtagEvent = function () { /* analytics not loaded */ };
}

// ----- CSRF helpers -----------------------------------------------------
// The server sets a non-HttpOnly cookie `cardtax_csrf` on every response.
// We read it and attach it as `X-CSRF-Token` on state-changing fetches.
function getCsrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)cardtax_csrf=([^;]+)/);
  if (m) return decodeURIComponent(m[1]);
  return "";
}

async function ensureCsrfToken() {
  // First page load on a brand-new session may not have the cookie yet — fall
  // back to a one-shot fetch that the middleware will populate.
  let t = getCsrfToken();
  if (t) return t;
  try {
    const r = await _origFetch("/api/csrf-token", { credentials: "same-origin" });
    if (r.ok) {
      const d = await r.json();
      return d.csrf_token || getCsrfToken() || "";
    }
  } catch (e) { /* offline; bail */ }
  return getCsrfToken();
}

const _UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const _origFetch = window.fetch.bind(window);

// Drop-in fetch replacement that auto-attaches the CSRF header on non-GETs.
async function ctxFetch(input, init) {
  init = init || {};
  const method = (init.method || "GET").toUpperCase();
  if (_UNSAFE_METHODS.has(method)) {
    const token = await ensureCsrfToken();
    const headers = new Headers(init.headers || {});
    if (token && !headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", token);
    init.headers = headers;
  }
  return _origFetch(input, init);
}

async function ctxFetchJSON(input, init) {
  const r = await ctxFetch(input, init);
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${r.status}: ${text}`);
  }
  return r.json();
}

// Monkey-patch the global fetch so existing template code picks up the CSRF
// header without per-site edits. Only attaches on same-origin, state-changing
// requests — GETs and cross-origin URLs (Stripe etc.) are untouched.
window.fetch = function (input, init) {
  init = init || {};
  const method = (init.method || (init.body ? "POST" : "GET")).toUpperCase();
  if (!_UNSAFE_METHODS.has(method)) return _origFetch(input, init);
  let sameOrigin = false;
  try {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    sameOrigin = url.startsWith("/") || url.startsWith(window.location.origin);
  } catch (e) { sameOrigin = false; }
  if (!sameOrigin) return _origFetch(input, init);
  const tok = getCsrfToken();
  if (tok) {
    const headers = new Headers(init.headers || {});
    if (!headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", tok);
    return _origFetch(input, { ...init, headers });
  }
  return ensureCsrfToken().then(t => {
    const headers = new Headers(init.headers || {});
    if (t && !headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", t);
    return _origFetch(input, { ...init, headers });
  });
};


const NAV = [
  { href: "/dashboard",    label: "Dashboard",       key: "dashboard" },
  { href: "/transactions", label: "Transactions",    key: "transactions" },
  { href: "/upload",       label: "Import",          key: "upload" },
  { href: "/connections",  label: "Connections",     key: "connections" },
  { href: "/scan",         label: "Scan",            key: "scan" },
  { href: "/tax-summary",  label: "Tax",             key: "tax-summary" },
  { href: "/quiz",         label: "Classification",  key: "quiz" },
  { href: "/tools",        label: "Tools",           key: "tools" },
  { href: "/settings",     label: "Settings",        key: "settings" },
];

const FOOTER_LINKS = [
  { href: "/pricing",        label: "Pricing",   key: "pricing" },
  { href: "/blog",           label: "Blog",      key: "blog" },
  { href: "/help",           label: "Help",      key: "help" },
  { href: "/changelog",      label: "Changelog", key: "changelog" },
  { href: "/terms",          label: "Terms",     key: "terms" },
  { href: "/privacy",        label: "Privacy",   key: "privacy" },
  { href: "/refund-policy",  label: "Refunds",   key: "refund-policy" },
];

const TIER_LABEL = { free: "Free", starter: "Starter", pro: "Pro", unlimited: "Unlimited" };

const ICONS = {
  search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
  inbox: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>',
  upload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>',
  camera: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>',
  moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
  sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="6.34" y2="6.34"/><line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="6.34" y2="17.66"/><line x1="17.66" y1="6.34" x2="19.07" y2="4.93"/></svg>',
};

// Register the service worker exactly once per page load. We attach the
// manifest link too — saves having to edit every template.
(function ensurePwa() {
  // Inject manifest link if the page didn't already declare one.
  if (!document.querySelector('link[rel="manifest"]')) {
    const link = document.createElement("link");
    link.rel = "manifest";
    link.href = "/manifest.json";
    document.head.appendChild(link);
  }
  if ("serviceWorker" in navigator && !window._ctxSWRegistered) {
    window._ctxSWRegistered = true;
    // Defer registration until idle so it never competes with first paint.
    const reg = () => navigator.serviceWorker.register("/sw.js").catch(() => {});
    if (document.readyState === "complete") reg();
    else window.addEventListener("load", reg);
  }
})();

function mountShell(activeKey) {
  // Theme init
  const saved = localStorage.getItem("cardtax-theme");
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  if (saved === "dark" || (saved !== "light" && prefersDark)) {
    document.documentElement.classList.add("dark");
  }

  const shell = document.getElementById("app-shell");
  if (!shell) return;

  shell.innerHTML = `
    <div class="app-shell">
      <header class="topbar">
        <div class="topbar-inner">
          <a href="/dashboard" class="brand">CardTax</a>
          <nav class="nav">
            ${NAV.map(n => `
              <a href="${n.href}" class="nav-link ${n.key === activeKey ? "active" : ""}">${n.label}</a>
            `).join("")}
          </nav>
          <button class="hamburger" id="hamburger-btn" aria-label="Open menu" aria-expanded="false" type="button">
            <span></span><span></span><span></span>
          </button>
          <div class="nav-right">
            <button class="theme-toggle" id="theme-toggle" aria-label="Toggle theme"></button>
            <div class="user-menu" id="user-menu" style="display:none">
              <button class="user-menu-btn" id="user-menu-btn" type="button" aria-haspopup="true" aria-expanded="false">
                <span class="user-menu-email" id="user-menu-email"></span>
                <span class="user-menu-tier" id="user-menu-tier"></span>
              </button>
              <div class="user-menu-dropdown" id="user-menu-dropdown" role="menu">
                <a href="/account" class="user-menu-item">Account & billing</a>
                <a href="/pricing" class="user-menu-item">Pricing</a>
                <div class="user-menu-divider"></div>
                <button type="button" class="user-menu-item user-menu-signout" id="user-menu-signout">Sign out</button>
              </div>
            </div>
          </div>
        </div>
      </header>

      <div class="mobile-drawer" id="mobile-drawer" aria-hidden="true">
        <div class="mobile-drawer-backdrop" id="mobile-drawer-backdrop"></div>
        <nav class="mobile-drawer-panel" aria-label="Mobile navigation">
          <div class="mobile-drawer-head">
            <a href="/dashboard" class="brand">CardTax</a>
            <button class="mobile-drawer-close" id="mobile-drawer-close" aria-label="Close menu">×</button>
          </div>
          <div class="mobile-drawer-links">
            ${NAV.map(n => `<a href="${n.href}" class="mobile-drawer-link ${n.key === activeKey ? "active" : ""}">${n.label}</a>`).join("")}
            <div style="height:1px;background:var(--rule);margin:12px 0"></div>
            <a href="/account" class="mobile-drawer-link">Account & billing</a>
            <a href="/pricing" class="mobile-drawer-link">Pricing</a>
            <button class="mobile-drawer-link mobile-drawer-signout" id="mobile-drawer-signout" type="button">Sign out</button>
          </div>
        </nav>
      </div>

      <main class="main">
        <div class="page">
          <div id="page-content"></div>
        </div>
      </main>

      <footer class="footer">
        <div class="footer-inner">
          <span>© ${new Date().getFullYear()} CardTax · Not tax advice. Consult a CPA.</span>
          <div class="footer-links">
            ${FOOTER_LINKS.map(l => `<a href="${l.href}">${l.label}</a>`).join("")}
          </div>
        </div>
      </footer>
    </div>
  `;

  // Mobile drawer
  const drawer = document.getElementById("mobile-drawer");
  const hambBtn = document.getElementById("hamburger-btn");
  const drawerClose = document.getElementById("mobile-drawer-close");
  const drawerBg = document.getElementById("mobile-drawer-backdrop");
  function openDrawer() {
    if (!drawer) return;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    hambBtn && hambBtn.setAttribute("aria-expanded", "true");
    document.body.style.overflow = "hidden";
  }
  function closeDrawer() {
    if (!drawer) return;
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    hambBtn && hambBtn.setAttribute("aria-expanded", "false");
    document.body.style.overflow = "";
  }
  if (hambBtn) hambBtn.addEventListener("click", openDrawer);
  if (drawerClose) drawerClose.addEventListener("click", closeDrawer);
  if (drawerBg) drawerBg.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });
  // Close the drawer if the user resizes past the mobile breakpoint —
  // otherwise it gets stuck open as a floating panel on desktop.
  window.addEventListener("resize", () => {
    if (window.innerWidth > 820 && drawer && drawer.classList.contains("open")) {
      closeDrawer();
    }
  });
  const drawerSignout = document.getElementById("mobile-drawer-signout");
  if (drawerSignout) drawerSignout.addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    window.location.href = "/login";
  });

  function updateThemeUI() {
    const dark = document.documentElement.classList.contains("dark");
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.innerHTML = dark ? ICONS.sun : ICONS.moon;
  }
  updateThemeUI();
  const tt = document.getElementById("theme-toggle");
  if (tt) tt.addEventListener("click", () => {
    const dark = document.documentElement.classList.toggle("dark");
    localStorage.setItem("cardtax-theme", dark ? "dark" : "light");
    updateThemeUI();
    // notify charts
    document.dispatchEvent(new CustomEvent("themechange"));
  });

  // Cookie consent (shows for first-time visitors). Loads analytics on accept.
  mountCookieBanner();

  // Load current user → populate the user menu in the top-right. Public pages
  // (blog, help, pricing, changelog, refund-policy) render fine without auth;
  // gated pages are protected server-side by `_gated_page`, so we don't need
  // to redirect from JS — just leave the user menu hidden when anonymous.
  const PUBLIC_PAGES = new Set([
    "blog", "help", "changelog", "refund-policy",
    "pricing", "terms", "privacy", "landing",
  ]);
  const isPublic = PUBLIC_PAGES.has(activeKey);

  fetch("/api/auth/me").then(r => {
    if (r.status === 401) {
      // Anonymous visitor on a gated page: send them to /login. On public
      // pages, just stay put.
      if (!isPublic) window.location.href = "/login";
      return null;
    }
    if (!r.ok) return null;
    return r.json();
  }).then(user => {
    if (!user) return;
    if (user.email_verified === false) mountVerifyBanner(user);
    const menu = document.getElementById("user-menu");
    document.getElementById("user-menu-email").textContent = user.email || "";
    const tierEl = document.getElementById("user-menu-tier");
    const tier = user.subscription_tier || "free";
    tierEl.textContent = TIER_LABEL[tier] || tier;
    tierEl.dataset.tier = tier;
    menu.style.display = "inline-flex";

    const btn = document.getElementById("user-menu-btn");
    const dd = document.getElementById("user-menu-dropdown");
    btn.addEventListener("click", e => {
      e.stopPropagation();
      const open = dd.classList.toggle("open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", e => {
      if (!menu.contains(e.target)) {
        dd.classList.remove("open");
        btn.setAttribute("aria-expanded", "false");
      }
    });
    document.getElementById("user-menu-signout").addEventListener("click", async () => {
      await fetch("/api/auth/logout", { method: "POST" });
      window.location.href = "/login";
    });

    // Mount the chat widget for authenticated users only.
    mountChatWidget();
  }).catch(() => {});
}

// HTML-escape user-controlled strings before splicing into innerHTML. Use
// anywhere transactional data (item titles, notes, etc.) is rendered.
function escHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "\"":"&quot;", "'":"&#39;" })[c]);
}

// ----- Email-verification banner -----
function mountVerifyBanner(user) {
  if (document.getElementById("verify-banner")) return;
  const bar = document.createElement("div");
  bar.id = "verify-banner";
  bar.setAttribute("role", "status");
  bar.style.cssText = "background:#fff4d6;border-bottom:1px solid #e3ddcd;color:#5a4a17;padding:10px 16px;font-size:13px;display:flex;align-items:center;justify-content:center;gap:14px;flex-wrap:wrap;text-align:center;";
  bar.innerHTML = `
    <span><strong>Please verify your email.</strong> Some features (PDF export, marketplace connections) are off until you do.</span>
    <button id="verify-resend" type="button" style="padding:5px 12px;border:1px solid #5a4a17;background:transparent;color:#5a4a17;border-radius:3px;font-size:12px;cursor:pointer">Resend verification</button>
    <span id="verify-status" style="font-size:12px"></span>
  `;
  document.body.insertBefore(bar, document.body.firstChild);
  document.getElementById("verify-resend").addEventListener("click", async () => {
    const stat = document.getElementById("verify-status");
    stat.textContent = "Sending…";
    try {
      const r = await ctxFetchJSON("/api/auth/resend-verification", { method: "POST" });
      if (r.already_verified) {
        stat.textContent = "Already verified — reload the page.";
      } else if (r.dev_verification_link) {
        stat.innerHTML = `Sent. Dev link: <a href="${r.dev_verification_link}">${r.dev_verification_link}</a>`;
      } else {
        stat.textContent = "Sent. Check your inbox.";
      }
    } catch (e) {
      stat.textContent = "Couldn't send — try again later.";
    }
  });
}

// ----- API helpers -----
async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (r.status === 401 && !path.startsWith("/api/auth/")) {
    // Session expired — kick back to /login
    window.location.href = "/login";
    throw new Error("401: not authenticated");
  }
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${r.status}: ${text}`);
  }
  return r.json();
}

// Extract a useful message from an error thrown by api() (status + JSON body).
function apiErrorMessage(err, fallback) {
  let msg = (err && err.message) || fallback || "Something went wrong.";
  try {
    const body = msg.replace(/^\d+:\s*/, "");
    const parsed = JSON.parse(body);
    if (typeof parsed.detail === "string") return parsed.detail;
    if (parsed.detail && typeof parsed.detail.message === "string") return parsed.detail.message;
    if (typeof parsed.detail === "object") return JSON.stringify(parsed.detail);
  } catch {}
  return msg;
}

// ----- Formatters -----
function fmt$(n) {
  if (n === null || n === undefined || isNaN(n)) return "$0.00";
  const neg = n < 0;
  const abs = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${neg ? "−" : ""}$${abs}`;
}
function fmt$0(n) {
  if (n === null || n === undefined || isNaN(n)) return "$0";
  const neg = n < 0;
  return `${neg ? "−" : ""}$${Math.abs(Math.round(n)).toLocaleString("en-US")}`;
}
function fmtPct(n) { if (!n) return "0%"; return (n * 100).toFixed(1) + "%"; }
function fmtPct2(n) { if (!n) return "0%"; return (n * 100).toFixed(2) + "%"; }
function fmtDate(d) {
  if (!d) return "—";
  const dt = new Date(d);
  if (isNaN(dt.getTime())) return d;
  return dt.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}
function gainCls(n) {
  if (n > 0.005) return "gain";
  if (n < -0.005) return "loss";
  return "muted";
}

// ----- Toast: stacked bottom-right, auto-dismiss after 4s -----
function _toastContainer() {
  let c = document.getElementById("toast-stack");
  if (!c) {
    c = document.createElement("div");
    c.id = "toast-stack";
    c.className = "toast-stack";
    document.body.appendChild(c);
  }
  return c;
}
function toast(msg, kind) {
  const container = _toastContainer();
  const t = document.createElement("div");
  t.className = "toast" + (kind ? " " + kind : "");
  t.textContent = msg;
  container.appendChild(t);
  // Force layout then add 'in' class for transition.
  requestAnimationFrame(() => t.classList.add("in"));
  const remove = () => {
    t.classList.remove("in");
    t.classList.add("out");
    setTimeout(() => t.remove(), 200);
  };
  setTimeout(remove, 4000);
  t.addEventListener("click", remove);
  return t;
}

// ----- Modal confirm: returns Promise<boolean> -----
function confirmModal(opts) {
  opts = typeof opts === "string" ? { message: opts } : (opts || {});
  return new Promise(resolve => {
    const back = document.createElement("div");
    back.className = "modal-backdrop";
    back.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-title">${(opts.title || "Are you sure?").replace(/</g,"&lt;")}</div>
        <div class="modal-body">${(opts.message || "").replace(/</g,"&lt;")}</div>
        <div class="modal-actions">
          <button class="btn btn-ghost" data-act="cancel" type="button">${opts.cancelLabel || "Cancel"}</button>
          <button class="btn ${opts.danger ? "btn-danger" : "btn-primary"}" data-act="ok" type="button">${opts.confirmLabel || "Continue"}</button>
        </div>
      </div>`;
    document.body.appendChild(back);
    requestAnimationFrame(() => back.classList.add("open"));
    function close(val) {
      back.classList.remove("open");
      setTimeout(() => back.remove(), 180);
      document.removeEventListener("keydown", onKey);
      resolve(val);
    }
    function onKey(e) {
      if (e.key === "Escape") close(false);
      if (e.key === "Enter") close(true);
    }
    document.addEventListener("keydown", onKey);
    back.addEventListener("click", e => { if (e.target === back) close(false); });
    back.querySelector('[data-act="cancel"]').addEventListener("click", () => close(false));
    back.querySelector('[data-act="ok"]').addEventListener("click", () => close(true));
    setTimeout(() => back.querySelector('[data-act="ok"]').focus(), 30);
  });
}

// ----- Button loading state: replaces text with spinner + label -----
function setBtnLoading(btn, loadingText) {
  if (!btn) return () => {};
  if (btn.dataset.loading === "1") return () => {};
  btn.dataset.loading = "1";
  btn.dataset.origLabel = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner" aria-hidden="true"></span><span>${loadingText || "Working…"}</span>`;
  return () => {
    btn.disabled = false;
    btn.innerHTML = btn.dataset.origLabel || "";
    delete btn.dataset.loading;
    delete btn.dataset.origLabel;
  };
}

// ----- Tooltip helper: returns inline HTML for a hover tooltip -----
function tt(text, label) {
  const safe = String(text || "").replace(/[&<>"']/g, c =>
    ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "\"":"&quot;", "'":"&#39;" })[c]);
  return `<span class="tt" tabindex="0">${label ? `<span>${label}</span>` : ""}<span class="tt-trigger" aria-hidden="true">?</span><span class="tt-body" role="tooltip">${safe}</span></span>`;
}

// Common tax-term tooltips reused across templates.
const TAX_TOOLTIPS = {
  FIFO: "First-in-first-out: when you sell a card, you assume you sold the earliest acquired copy first. IRS default.",
  LIFO: "Last-in-first-out: assumes the most recently acquired copy was sold first.",
  "Cost Basis": "What you can subtract from sale price before tax is calculated — purchase price, grading fees, shipping in.",
  NIIT: "Net Investment Income Tax — 3.8% extra federal tax on investment income above MAGI thresholds ($200k single, $250k MFJ).",
  "Capital Gains": "Profit from selling an asset for more than your basis. Short-term (≤1 yr) taxed as ordinary income; long-term taxed at preferential rates.",
  "Schedule C": "Form 1040 Schedule C — Profit or Loss from Business. Required if you're classified as a dealer.",
  "Schedule D": "Form 1040 Schedule D — Capital Gains and Losses. Required if you're classified as an investor.",
  "Form 8949": "Sales and Other Dispositions of Capital Assets. Itemizes each capital gain/loss line that flows to Schedule D.",
  Collectibles: "IRC §408(m): trading cards are 'collectibles'. Long-term gains are taxed at up to 28% (vs. 15-20% for stocks).",
  "Holding Period": "Days between purchase and sale. >365 days → long-term; ≤365 → short-term.",
  "Wash Sale": "Selling at a loss then rebuying within 30 days. The rule applies to stocks/securities — collectibles are NOT subject to wash sale rules (IRC §1091).",
  "QBI Deduction": "Qualified Business Income deduction (§199A): 20% deduction on dealer net profit, phased out above income thresholds.",
  "Self-Employment Tax": "15.3% combined Social Security + Medicare on dealer net profit (Schedule SE).",
  "1099-K": "Form issued by payment platforms (eBay, Whatnot, PayPal) reporting your gross sales. 2026 threshold: $600.",
};

// Render the disclaimer footer used on tax-summary and PDF pages.
function disclaimerFooter() {
  return `
    <div class="disclaimer-footer">
      <strong>Disclaimer:</strong> CardTax provides tax calculations for informational purposes only.
      This is not tax advice. Consult a qualified tax professional before filing.
      CardTax is not responsible for errors in tax calculations.
    </div>`;
}

// ----- Cookie consent banner -----
function mountCookieBanner() {
  if (localStorage.getItem("cardtax-cookies") !== null) return;
  const bar = document.createElement("div");
  bar.className = "cookie-banner";
  bar.innerHTML = `
    <div class="cookie-banner-inner">
      <div class="cookie-text">
        We use cookies to improve your experience and (if you accept) measure usage with Google Analytics.
        <a href="/privacy">Privacy</a>
      </div>
      <div class="cookie-actions">
        <button class="btn btn-ghost btn-sm" data-act="decline" type="button">Decline</button>
        <button class="btn btn-primary btn-sm" data-act="accept" type="button">Accept</button>
      </div>
    </div>`;
  document.body.appendChild(bar);
  function set(value) {
    localStorage.setItem("cardtax-cookies", value);
    bar.classList.add("hide");
    setTimeout(() => bar.remove(), 200);
    if (value === "accept" && typeof window.loadAnalytics === "function") {
      window.loadAnalytics();
    }
  }
  bar.querySelector('[data-act="accept"]').addEventListener("click", () => set("accept"));
  bar.querySelector('[data-act="decline"]').addEventListener("click", () => set("decline"));
  requestAnimationFrame(() => bar.classList.add("open"));
}

// ============================================================
// Chat widget — floating bubble + side panel
// Only mounts for authenticated users; silently skips otherwise.
// ============================================================

const CHAT_ICON = `<svg viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>`;

function mountChatWidget() {
  if (document.getElementById("chat-launcher")) return;  // already mounted

  const launcher = document.createElement("button");
  launcher.id = "chat-launcher";
  launcher.className = "chat-launcher";
  launcher.setAttribute("aria-label", "Open chat");
  launcher.innerHTML = `${CHAT_ICON}<span class="chat-launcher-dot" aria-hidden="true"></span>`;

  const panel = document.createElement("div");
  panel.id = "chat-panel";
  panel.className = "chat-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "CardTax chat");
  panel.innerHTML = `
    <div class="chat-header">
      <div>
        <div class="chat-header-title">CardTax help</div>
        <div class="chat-header-sub">Ask anything about card-sale taxes.</div>
      </div>
      <button class="chat-close" id="chat-close" aria-label="Close chat">×</button>
    </div>
    <div class="chat-body" id="chat-body"></div>
    <div class="chat-input-bar">
      <div class="chat-escalate-row">
        <button class="chat-escalate-link" id="chat-escalate-link" type="button">Talk to a person</button>
        <span class="chat-escalate-status" id="chat-escalate-status"></span>
      </div>
      <div class="chat-escalate-form" id="chat-escalate-form">
        <input type="email" id="chat-escalate-email" placeholder="Email for follow-up (optional)" autocomplete="email">
      </div>
      <div class="chat-input-row">
        <textarea class="chat-textarea" id="chat-input" rows="1" placeholder="Type a question…"></textarea>
        <button class="chat-send" id="chat-send" type="button">Send</button>
      </div>
    </div>
  `;

  document.body.appendChild(launcher);
  document.body.appendChild(panel);

  const state = {
    messages: [],
    suggestions: [],
    open: false,
    escalateMode: false,
    loaded: false,
    sending: false,
  };

  const body     = panel.querySelector("#chat-body");
  const input    = panel.querySelector("#chat-input");
  const sendBtn  = panel.querySelector("#chat-send");
  const escLink  = panel.querySelector("#chat-escalate-link");
  const escForm  = panel.querySelector("#chat-escalate-form");
  const escEmail = panel.querySelector("#chat-escalate-email");
  const escStat  = panel.querySelector("#chat-escalate-status");
  const closeBtn = panel.querySelector("#chat-close");

  function esc(s) {
    return String(s || "").replace(/[&<>"']/g, c =>
      ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "\"":"&quot;", "'":"&#39;" })[c]);
  }
  function fmtTime(iso) {
    if (!iso) return "";
    const dt = new Date(iso);
    if (isNaN(dt.getTime())) return "";
    return dt.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  }
  function roleLabel(role) {
    if (role === "user") return "You";
    if (role === "admin") return "CardTax team";
    return "Auto-reply";
  }

  function renderBody() {
    const parts = [];
    if (!state.messages.length) {
      parts.push(`
        <div class="chat-welcome">
          <div class="chat-welcome-title">Hi — how can we help?</div>
          We answer most tax questions instantly. If we can't, your message goes to the team.
          <div class="chat-suggestions" id="chat-suggestions">
            ${state.suggestions.slice(0, 6).map(s => `
              <button class="chat-suggestion" data-key="${s.key}" type="button">${esc(s.question)}</button>
            `).join("")}
          </div>
        </div>
      `);
    }
    state.messages.forEach(m => {
      parts.push(`
        <div class="chat-row ${m.role}">
          <div class="chat-bubble">${esc(m.content)}</div>
          <div class="chat-meta">${roleLabel(m.role)} · ${fmtTime(m.created_at)}</div>
        </div>
      `);
    });
    body.innerHTML = parts.join("");
    body.scrollTop = body.scrollHeight;

    body.querySelectorAll(".chat-suggestion").forEach(b => {
      b.addEventListener("click", () => {
        const sug = state.suggestions.find(s => s.key === b.dataset.key);
        if (!sug) return;
        sendMessage(sug.question, b.dataset.key);
      });
    });
  }

  function updateUnreadDot() {
    const unread = state.messages.some(m => m.role === "admin" && m._unread);
    launcher.classList.toggle("has-unread", unread);
  }

  async function loadHistory() {
    try {
      const r = await fetch("/api/chat/history");
      if (r.status === 401) { return false; }
      if (!r.ok) return false;
      const data = await r.json();
      state.suggestions = data.suggestions || [];
      // Flag admin messages as unread if the server says there are any —
      // we mark every admin message; mark-read will clear them on open.
      const adminIds = (data.messages || [])
        .filter(m => m.role === "admin").map(m => m.id).sort((a, b) => a - b);
      const unreadCount = data.unread_admin_replies || 0;
      const unreadIds = new Set(adminIds.slice(-unreadCount));
      state.messages = (data.messages || []).map(m => ({
        ...m,
        _unread: m.role === "admin" && unreadIds.has(m.id),
      }));
      state.loaded = true;
      renderBody();
      updateUnreadDot();
      return true;
    } catch (e) {
      return false;
    }
  }

  async function sendMessage(text, suggestionKey) {
    text = (text || "").trim();
    if (!text || state.sending) return;
    state.sending = true;
    sendBtn.disabled = true;
    const optimistic = {
      id: -Date.now(),
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
    };
    state.messages.push(optimistic);
    renderBody();
    try {
      const r = await fetch("/api/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, suggestion_key: suggestionKey || null }),
      });
      if (!r.ok) throw new Error(await r.text());
      const data = await r.json();
      // Drop the optimistic msg and replace with server-side records.
      state.messages = state.messages.filter(m => m.id !== optimistic.id);
      state.messages.push(...(data.messages || []));
      if (!data.matched) {
        // Auto-suggest escalation if no KB hit.
        escStat.textContent = "Couldn't auto-answer — click \"Talk to a person\" to send it to the team.";
      }
      renderBody();
    } catch (e) {
      state.messages = state.messages.filter(m => m.id !== optimistic.id);
      state.messages.push({
        id: -Date.now(),
        role: "bot",
        content: "Something went wrong sending that. Try again in a moment.",
        created_at: new Date().toISOString(),
      });
      renderBody();
    } finally {
      state.sending = false;
      sendBtn.disabled = false;
    }
  }

  async function escalate() {
    const text = (input.value || "").trim();
    if (!text) {
      escStat.textContent = "Type your question first, then click \"Talk to a person.\"";
      input.focus();
      return;
    }
    if (state.sending) return;
    state.sending = true;
    sendBtn.disabled = true;
    escStat.textContent = "Sending…";
    try {
      const r = await fetch("/api/chat/escalate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, email: escEmail.value.trim() || null }),
      });
      if (!r.ok) throw new Error(await r.text());
      const data = await r.json();
      state.messages.push(...(data.messages || []));
      input.value = "";
      autosize();
      escStat.textContent = "Sent. You'll see the reply here.";
      escForm.classList.remove("open");
      state.escalateMode = false;
      renderBody();
    } catch (e) {
      escStat.textContent = "Failed to send — try again.";
    } finally {
      state.sending = false;
      sendBtn.disabled = false;
    }
  }

  async function markAdminRepliesRead() {
    const unread = state.messages.filter(m => m.role === "admin" && m._unread);
    if (!unread.length) return;
    const maxId = Math.max(...unread.map(m => m.id));
    try {
      await fetch("/api/chat/mark-read", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ last_id: maxId }),
      });
      state.messages.forEach(m => { if (m.role === "admin") m._unread = false; });
      updateUnreadDot();
    } catch (e) { /* non-fatal */ }
  }

  function openPanel() {
    state.open = true;
    panel.classList.add("open");
    if (typeof window.gtagEvent === "function") window.gtagEvent("chat_widget_open");
    if (!state.loaded) {
      loadHistory().then(() => {
        setTimeout(markAdminRepliesRead, 400);
      });
    } else {
      setTimeout(markAdminRepliesRead, 400);
    }
    setTimeout(() => input.focus(), 80);
  }
  function closePanel() {
    state.open = false;
    panel.classList.remove("open");
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(110, input.scrollHeight) + "px";
  }

  launcher.addEventListener("click", () => state.open ? closePanel() : openPanel());
  closeBtn.addEventListener("click", closePanel);
  sendBtn.addEventListener("click", () => {
    if (state.escalateMode) {
      escalate();
    } else {
      const txt = input.value.trim();
      if (!txt) return;
      input.value = "";
      autosize();
      escStat.textContent = "";
      sendMessage(txt, null);
    }
  });
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendBtn.click();
    }
  });
  escLink.addEventListener("click", () => {
    state.escalateMode = !state.escalateMode;
    escForm.classList.toggle("open", state.escalateMode);
    if (state.escalateMode) {
      sendBtn.textContent = "Send to team";
      escLink.textContent = "Cancel";
      escStat.textContent = "Your message and email go to the CardTax team.";
      input.focus();
    } else {
      sendBtn.textContent = "Send";
      escLink.textContent = "Talk to a person";
      escStat.textContent = "";
    }
  });

  // Initial load so the unread dot appears before the user opens the panel.
  loadHistory();
}

// ----- Chart helpers -----
function chartTheme() {
  const dark = document.documentElement.classList.contains("dark");
  const css = getComputedStyle(document.documentElement);
  const ink   = css.getPropertyValue("--ink").trim();
  const mute  = css.getPropertyValue("--ink-mute").trim();
  const rule  = css.getPropertyValue("--rule").trim();
  const paper = css.getPropertyValue("--paper").trim();
  return {
    text:    mute,
    grid:    dark ? "rgba(255,255,255,0.05)" : "rgba(26,26,23,0.06)",
    border:  rule,
    accent:  ink,
    paper:   paper,
    palette: [ink, "#7a5c3a", "#3c5a78", "#7a3c4c", "#5e6b3a", "#4a3c5c", "#7d6a3a"],
  };
}
