/* FRIDAY V4 — Companion PWA service worker.
   Caches ONLY the app shell (HTML/CSS/JS/manifest/icons) so the UI
   opens instantly and offline. Every /api/* request — status, talk,
   conversation, the SSE event stream, pairing — is ALWAYS passed
   straight to the network: Friday's state is live and must never be
   served stale from a cache. */
"use strict";

const CACHE = "friday-companion-v1";
const SHELL = ["/", "/index.html", "/app.css", "/app.js", "/manifest.json",
               "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;         // same-origin only
  if (url.pathname.startsWith("/api/")) return;            // live data — never cache

  if (event.request.method !== "GET") return;
  event.respondWith(
    caches.match(event.request).then((hit) => {
      if (hit) return hit;
      return fetch(event.request).then((resp) => {
        if (resp.ok && url.pathname !== "/") {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
        }
        return resp;
      }).catch(() => caches.match("/"));                   // offline → shell
    })
  );
});
