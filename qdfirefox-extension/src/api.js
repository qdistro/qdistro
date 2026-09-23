// Firefox WebExtension API surface. Firefox exposes both `browser.*`
// (Promise-returning, the spec API) and `chrome.*` (callback-style,
// for Chrome-extension portability). We bind to `browser.*` so the
// rest of the source can `await api.tabs.query({})` without callback
// wrappers — that's the whole point of having a Firefox-native
// extension separate from qdchrome-extension.
//
// Exposed as `self.qdistroApi` so the IIFE modules can pick it up
// without ES-module imports across the event-page boundary. The
// build is a flat list of <script> entries in manifest.json; load
// order is dictated by manifest.background.scripts.
//
// @ts-check
(function (root) {
  "use strict";
  if (typeof browser === "undefined") {
    // Tests inject a synthetic `browser` onto the scope; if we're in
    // a real Chromium worker (we shouldn't be — this repo is
    // Firefox-only) we fall back to `chrome` to avoid a hard crash.
    root.qdistroApi = (typeof chrome !== "undefined") ? chrome : null;
    return;
  }
  root.qdistroApi = browser;
})(typeof self !== "undefined" ? self : globalThis);
