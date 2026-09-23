// screenlock module.
//
// Detects fullscreen video / presentation mode and sends
// `screenlock.inhibit` / `screenlock.release` to the bridge. The
// daemon-side policy decides whether the compositor honors the
// request.
//
// Implementation note: browser.windows.onFocusChanged + a
// content-script fullscreen observer is the cleanest path; for the
// MV3 scaffolding we expose the outbound shape and let a future
// content-script land the observer.
//
// @ts-check
(function (root) {
  "use strict";
  const dispatcher = root.qdistroDispatcher;

  function inhibit(reason) {
    return dispatcher.request("screenlock.inhibit", {
      reason: String(reason || "fullscreen_video"),
    });
  }

  function release(reason) {
    return dispatcher.request("screenlock.release", {
      reason: String(reason || "fullscreen_exit"),
    });
  }

  root.qdistroScreenlock = { inhibit, release };
})(typeof self !== "undefined" ? self : globalThis);
