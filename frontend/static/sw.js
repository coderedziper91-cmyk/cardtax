// CardTax service worker — caches the app shell so the navigation works
// instantly on repeat visits and survives brief offline windows.
//
// Cache-first for static assets; network-first for HTML pages so users see
// fresh content when online. Bump CACHE_VERSION whenever shipped assets
// change shape.

const CACHE_VERSION = "cardtax-v1";
const APP_SHELL = [
  "/static/app.css",
  "/static/shared.js",
  "/static/analytics.js",
  "/static/favicon.svg",
  "/static/apple-touch-icon.png",
  "/manifest.json",
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then(cache => cache.addAll(APP_SHELL))
      .catch(() => { /* offline at install time — fine, we'll backfill */ })
  );
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Never cache API or auth flows — the data has to be live.
  if (url.pathname.startsWith("/api/") ||
      url.pathname.startsWith("/auth/") ||
      url.pathname.startsWith("/admin")) {
    return;
  }

  // Cache-first for /static + manifest + icons.
  const isAsset =
    url.pathname.startsWith("/static/") ||
    url.pathname === "/manifest.json" ||
    url.pathname === "/favicon.svg" ||
    url.pathname === "/favicon.ico" ||
    url.pathname === "/apple-touch-icon.png";

  if (isAsset) {
    event.respondWith(
      caches.match(req).then(hit => hit || fetch(req).then(resp => {
        if (resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE_VERSION).then(c => c.put(req, copy));
        }
        return resp;
      }))
    );
    return;
  }

  // Network-first for HTML — fall back to the cached copy when offline.
  if (req.headers.get("accept") && req.headers.get("accept").includes("text/html")) {
    event.respondWith(
      fetch(req).then(resp => {
        if (resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE_VERSION).then(c => c.put(req, copy));
        }
        return resp;
      }).catch(() => caches.match(req).then(hit => hit ||
        caches.match("/static/app.css").then(() => new Response(
          "<h1>Offline</h1><p>CardTax is unavailable. Reconnect and refresh.</p>",
          { headers: { "Content-Type": "text/html" } }
        ))
      ))
    );
  }
});
