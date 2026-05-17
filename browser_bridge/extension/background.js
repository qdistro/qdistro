// qdistro browser-bridge MV3 service-worker stub.
//
// MV3 mandates a service worker; for the Phase-8 MVP we don't keep
// a persistent port (no streaming ops). Service worker is empty
// modulo a no-op install handler so Chromium's installer accepts
// the manifest. Persistent-port + 25s heartbeat per spec/14
// §"Heartbeat for persistent ports" lands in Phase-9 with the
// first streaming op (e.g. tabs.list).

self.addEventListener("install", () => {
  // No-op. The service worker has nothing to do in MVP — the popup
  // owns the connectNative round-trip directly.
});
