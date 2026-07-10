// Minimal service worker — its only job is to make the hosted join page an
// installable PWA (a registered SW with a fetch handler is the installability
// requirement). It's a pass-through: no caching, so it never serves stale
// contributor state or shadows the coordination server's live data.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {
  // Intentionally empty: fall through to the network for every request.
});
