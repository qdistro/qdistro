// mpris content-script.
//
// Observes navigator.mediaSession metadata + playback state and
// reports changes to the background event page, which forwards
// them to the bridge as mpris.update. Also receives `mpris.do_action`
// from the background and invokes the corresponding mediaSession
// action handler (or falls back to direct <audio>/<video> control).
//
// Media Session API has no event for metadata changes; we 1Hz-poll
// for shape changes. The poll is gated on at least one media
// element being present to avoid burning CPU on text-only pages.
//
// Open items in todo/04-mpris-content-script.md (cross-frame,
// multi-tab arbitration, album art fetching, event-based polling).
//
// @ts-check
(function () {
  "use strict";
  const api = (typeof browser !== "undefined") ? browser : chrome;
  if (!api || !api.runtime) return;

  const POLL_INTERVAL_MS = 1000;
  let pollTimer = null;
  let lastSnapshot = "";

  function log(...args) {
    try { console.debug("[qdistro/mpris-content]", ...args); } catch (_) {}
  }

  function hasMediaElement() {
    return !!document.querySelector("audio, video");
  }

  function snapshot() {
    const m = (navigator.mediaSession && navigator.mediaSession.metadata) || null;
    const state = (navigator.mediaSession && navigator.mediaSession.playbackState) || "none";
    const artwork = m && Array.isArray(m.artwork) && m.artwork.length > 0 ? m.artwork[0] : null;
    const media = document.querySelector("audio, video");
    return {
      title: (m && m.title) || (document.title || ""),
      artist: (m && m.artist) || "",
      album: (m && m.album) || "",
      art_url: (artwork && artwork.src) || "",
      state, // "playing" | "paused" | "none"
      position: media ? Math.floor(media.currentTime || 0) : null,
      duration: media && isFinite(media.duration) ? Math.floor(media.duration) : null,
      url: location.href,
    };
  }

  async function reportIfChanged() {
    if (!hasMediaElement()) return;
    const snap = snapshot();
    const key = JSON.stringify({
      t: snap.title, a: snap.artist, s: snap.state, u: snap.url,
    });
    if (key === lastSnapshot) return;
    lastSnapshot = key;
    try {
      await api.runtime.sendMessage({ kind: "mpris.report_update", ...snap });
    } catch (e) {
      log("report failed", e && e.message);
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(reportIfChanged, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (!pollTimer) return;
    clearInterval(pollTimer);
    pollTimer = null;
  }

  // Drive an event-based update on play/pause as well — cheaper than
  // waiting for the poll tick to notice a state change.
  function attachMediaListeners(el) {
    if (el.__qdistroMprisHooked) return;
    el.__qdistroMprisHooked = true;
    for (const ev of ["play", "pause", "ended", "loadedmetadata"]) {
      el.addEventListener(ev, () => { lastSnapshot = ""; reportIfChanged(); });
    }
  }

  function rescanMedia() {
    const els = document.querySelectorAll("audio, video");
    if (els.length === 0) {
      stopPolling();
      return;
    }
    els.forEach(attachMediaListeners);
    startPolling();
  }

  // Observe DOM mutations for late-added media elements (single-page
  // apps load media after first paint).
  const mo = new MutationObserver(() => rescanMedia());
  mo.observe(document.documentElement, { childList: true, subtree: true });
  rescanMedia();

  // Inbound — bridge wants us to control the page's player.
  api.runtime.onMessage.addListener((req, _sender) => {
    if (!req || req.kind !== "mpris.do_action") return undefined;
    const action = String(req.action || "");
    log("do_action", action);
    const ms = navigator.mediaSession;
    if (ms && typeof ms.setActionHandler === "function") {
      // We can't invoke a page's registered action handler from a
      // content script — there's no API for that. Fall back to
      // direct HTMLMediaElement control.
    }
    const media = document.querySelector("audio, video");
    if (!media) return Promise.resolve({ ok: false, error: "no_media_element" });
    try {
      switch (action) {
        case "play":
          media.play();
          break;
        case "pause":
          media.pause();
          break;
        case "playpause":
          // The MPRIS PlayPause verb — toggle on the element's own
          // state so a single admin-widget button works.
          if (media.paused) media.play(); else media.pause();
          break;
        case "stop":
          // MPRIS Stop: pause and rewind to the start.
          media.pause();
          try { media.currentTime = 0; } catch (_) { /* live stream */ }
          break;
        case "seek":
          if (typeof req.value === "number") media.currentTime = req.value;
          break;
        case "next":
        case "previous":
          // Not portable without page cooperation; report and let
          // the bridge log a no-op.
          return Promise.resolve({ ok: false, error: "action_unsupported_by_page" });
        default:
          return Promise.resolve({ ok: false, error: "unknown_action" });
      }
      return Promise.resolve({ ok: true, action });
    } catch (e) {
      return Promise.resolve({ ok: false, error: String(e.message || e) });
    }
  });

  log("mpris content-script loaded");
})();
