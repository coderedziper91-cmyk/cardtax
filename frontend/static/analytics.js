// CardTax — Google Analytics (GA4) loader + conversion funnel events.
// Only loads when GOOGLE_ANALYTICS_ID is set on the server AND the user has
// accepted cookies (localStorage.cardtax-cookies === "accept"). Until that's
// true, gtagEvent queues calls.

(function () {
  if (window._ctxGaInit) return;
  window._ctxGaInit = true;
  window._ctxGaQueue = window._ctxGaQueue || [];

  window.gtagEvent = function (name, params) {
    try {
      if (typeof window.gtag === "function") {
        window.gtag("event", name, params || {});
      } else {
        window._ctxGaQueue.push([name, params || {}]);
      }
    } catch (e) { /* never crash on analytics */ }
  };

  // ---------------------------------------------------------------------
  // Conversion funnel — fire each stage exactly once per session/device so
  // GA4 doesn't double-count if the user lands on the same page twice.
  // ---------------------------------------------------------------------
  function _fireOnce(key, name, params) {
    try {
      if (sessionStorage.getItem("ctx-funnel:" + key)) return;
      sessionStorage.setItem("ctx-funnel:" + key, "1");
    } catch (e) { /* sessionStorage may be unavailable */ }
    window.gtagEvent(name, params || {});
  }

  window.cardtaxFunnel = {
    landingView:        function (p) { _fireOnce("landing", "page_view", Object.assign({ page: "landing" }, p || {})); },
    signUpStart:        function (p) { _fireOnce("signup_start", "sign_up_start", p || {}); },
    signUpComplete:     function (p) { window.gtagEvent("sign_up_complete", p || {}); },
    onboardingStart:    function (p) { _fireOnce("onb_start", "onboarding_start", p || {}); },
    onboardingComplete: function (p) { window.gtagEvent("onboarding_complete", p || {}); },
    firstUpload:        function (p) {
      try {
        if (localStorage.getItem("ctx-funnel:first_upload")) return;
        localStorage.setItem("ctx-funnel:first_upload", String(Date.now()));
      } catch (e) {}
      window.gtagEvent("first_upload", p || {});
    },
    firstTaxCalc: function (p) {
      try {
        if (localStorage.getItem("ctx-funnel:first_tax_calc")) return;
        localStorage.setItem("ctx-funnel:first_tax_calc", String(Date.now()));
      } catch (e) {}
      window.gtagEvent("first_tax_calc", p || {});
    },
    upgradeStart:    function (p) { window.gtagEvent("upgrade_start", p || {}); },
    upgradeComplete: function (p) { window.gtagEvent("upgrade_complete", p || {}); },
  };

  function actuallyLoad() {
    if (window._ctxGaLoaded) return;
    window._ctxGaLoaded = true;
    fetch("/api/site-config").then(r => r.ok ? r.json() : null).then(cfg => {
      if (!cfg || !cfg.google_analytics_id) return;
      const id = cfg.google_analytics_id;
      const s = document.createElement("script");
      s.async = true;
      s.src = "https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(id);
      document.head.appendChild(s);
      window.dataLayer = window.dataLayer || [];
      window.gtag = function () { window.dataLayer.push(arguments); };
      window.gtag("js", new Date());
      window.gtag("config", id, { send_page_view: true });
      (window._ctxGaQueue || []).forEach(([name, params]) => {
        try { window.gtag("event", name, params); } catch (e) {}
      });
      window._ctxGaQueue = [];
    }).catch(() => {});
  }

  // Public hook used by the cookie banner.
  window.loadAnalytics = actuallyLoad;

  // Auto-load if the user previously accepted cookies on this device.
  try {
    if (localStorage.getItem("cardtax-cookies") === "accept") actuallyLoad();
  } catch (e) { /* localStorage may be unavailable */ }
})();
