// Cross-browser WebExtension API shim. Both Firefox MV2/MV3 and
// Chromium MV3 expose chrome.* synchronously; Firefox additionally
// exposes browser.* with Promise-returning variants. We standardize
// on chrome.* with callbacks so the same source runs both targets.
// The bridge protocol does not care which API surface we use — the
// only WebExtension API we need at module scope is runtime.connectNative,
// and both browsers spell that identically.
//
// Exported as a global on `self` for service-worker compatibility
// (no ES module imports across MV2/MV3 boundary).
//
// @ts-check
(function (root) {
  "use strict";
  const api = (typeof browser !== "undefined") ? browser : chrome;
  root.qdistroApi = api;
})(typeof self !== "undefined" ? self : globalThis);
