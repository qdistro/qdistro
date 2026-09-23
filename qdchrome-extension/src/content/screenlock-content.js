// screenlock content-script.
//
// Listens for `fullscreenchange` and reports inhibit/release to the
// background. The bridge forwards to the compositor's idle-inhibit
// protocol (Wayland idle-inhibit-unstable-v1).
//
// Classification:
//   - fullscreen element contains a playing <video>  → fullscreen_video
//   - anything else                                  → fullscreen_presentation
//
// On `pagehide` (tab close, navigation away) we proactively release
// any active inhibit so the compositor doesn't get stuck holding the
// screen on.
//
// Open items in todo/05-screenlock-content-script.md (multi-tab
// inhibit reference counting, picture-in-picture, Wake Lock API
// interception).
//
// @ts-check
(function () {
  "use strict";
  const api = (typeof browser !== "undefined") ? browser : chrome;
  if (!api || !api.runtime) return;

  let activeInhibit = false;

  function log(...args) {
    try { console.debug("[qdistro/screenlock-content]", ...args); } catch (_) {}
  }

  function classifyFullscreenElement(el) {
    if (!el) return null;
    if (el.tagName === "VIDEO" && !el.paused) return "fullscreen_video";
    const innerVideo = el.querySelector && el.querySelector("video");
    if (innerVideo && !innerVideo.paused) return "fullscreen_video";
    return "fullscreen_presentation";
  }

  function send(kind, body) {
    api.runtime.sendMessage(Object.assign({ kind }, body || {}))
      .catch((e) => log("send failed", kind, e && e.message));
  }

  function onFullscreenChange() {
    const fsEl = document.fullscreenElement;
    if (fsEl) {
      const reason = classifyFullscreenElement(fsEl) || "fullscreen_presentation";
      log("entering fullscreen", reason);
      activeInhibit = true;
      send("screenlock.report_inhibit", { reason, tab_url: location.href });
    } else if (activeInhibit) {
      log("leaving fullscreen");
      activeInhibit = false;
      send("screenlock.report_release", { reason: "fullscreen_exit" });
    }
  }

  function onPageHide() {
    if (activeInhibit) {
      log("page hide while inhibit active; releasing");
      activeInhibit = false;
      send("screenlock.report_release", { reason: "tab_unload" });
    }
  }

  document.addEventListener("fullscreenchange", onFullscreenChange);
  // Firefox uses an unprefixed event but older content may use the
  // webkit-prefixed one; bind both for safety.
  document.addEventListener("webkitfullscreenchange", onFullscreenChange);
  window.addEventListener("pagehide", onPageHide);

  log("screenlock content-script loaded");
})();
