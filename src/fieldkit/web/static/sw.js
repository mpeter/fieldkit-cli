/* fieldkit service worker — offline shell + last-known brief. */
"use strict";

// Bump on any SHELL change: the shell is served cache-first, and only a changed
// worker reinstalls and drops the previous cache.
const CACHE = "fieldkit-v4";
const SHELL = ["/", "/static/style.css", "/static/app.js", "/static/icon.svg", "/manifest.json"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

/* Network-first for API (fresh data, cached fallback for offline brief reads);
   cache-first for the static shell. Never cache /events (SSE) or POSTs. */
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname === "/events") return;

  if (url.pathname.startsWith("/api/")) {
    e.respondWith(
      fetch(e.request)
        .then(res => {
          const copy = res.clone();
          // Cache.put() ignores Cache-Control — no-store must be checked manually (implementation change).
          if (!res.headers.get("Cache-Control")?.includes("no-store")) {
            caches.open(CACHE).then(c => c.put(e.request, copy));
          }
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
});
