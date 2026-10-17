/* Only the public app shell is cached. Never cache APIs, media or identities. */
const SHELL = "camera-shell-v1";
const FILES = ["/", "/app.js", "/style.css", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"];
self.addEventListener("install", event => {
  event.waitUntil(caches.open(SHELL).then(cache => cache.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", event => {
  // Reuse one shell cache across releases; install.addAll refreshes it atomically.
  event.waitUntil(self.clients.claim());
});
self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || !FILES.includes(url.pathname)) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok) {
      const copy = response.clone();
      event.waitUntil(caches.open(SHELL).then(cache => cache.put(event.request, copy)));
    }
    return response;
  }).catch(() => caches.match(url.pathname)));
});
