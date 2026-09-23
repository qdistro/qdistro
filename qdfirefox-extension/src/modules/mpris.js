// mpris module.
//
// Inbound `mpris.control` (play/pause/playpause/next/previous/seek):
// the admin media widget drives org.mpris.MediaPlayer2 controls which
// qdistro_mpris_daemon routes back to the originating browser tab via
// the bridge's RequestTabs surface as an `mpris.control` op. We forward
// it down to that tab's content/mpris-content.js as `mpris.do_action`
// and return the content script's real result — NOT a stub ack (a
// success-looking stub silently dropped every Play/Pause).
//
// Tab targeting: the daemon binds the control to the publishing tab, so
// `msg.tab_id` is authoritative when present. Falling back to the
// last-published tab keeps a control working for a publisher that
// reported without a tab_id; with neither, there's no addressable
// player, so we fail closed (`no_target_tab`) rather than guess the
// active tab — a control must hit the tab that owns the media session.
//
// Outbound: wire op is `mpris.publish` (NOT `mpris.update`) per the
// bridge handler's whitelist: (title, artist, album, playback_status,
// position_us, tab_id). The content script reports {state, position
// in seconds}; we translate here so the content script doesn't need
// to know the bridge's field naming.
//
// @ts-check
(function (root) {
  "use strict";
  const dispatcher = root.qdistroDispatcher;

  // Last tab that published a player update — the fallback control
  // target when the daemon omits an explicit tab_id.
  let lastPublishedTabId = null;

  dispatcher.register("mpris.control", async (msg) => {
    const action = String(msg.action || "");
    const tabId = typeof msg.tab_id === "number" ? msg.tab_id
      : (typeof lastPublishedTabId === "number" ? lastPublishedTabId : null);
    if (tabId == null) {
      return { ok: false, action, error: "no_target_tab" };
    }
    const tabs = root.qdistroTabs;
    if (!tabs || typeof tabs.sendMessageToTab !== "function") {
      return { ok: false, action, error: "tabs_unavailable" };
    }
    const forward = { kind: "mpris.do_action", action };
    // `seek` carries a numeric target time the content script applies
    // to media.currentTime.
    if (typeof msg.value === "number") forward.value = msg.value;
    try {
      const reply = await tabs.sendMessageToTab(tabId, forward);
      // A content script that received the message returns
      // {ok, action, ...}; a tab with no content script (or a
      // disconnected reply) yields undefined — surface that as a
      // deterministic failure rather than a phantom success.
      if (reply && typeof reply === "object") return reply;
      return { ok: false, action, error: "no_content_script" };
    } catch (e) {
      return {
        ok: false,
        action,
        error: "tab_delivery_failed",
        detail: String((e && e.message) || e).slice(0, 200),
      };
    }
  });

  function update(payload) {
    payload = payload || {};
    if (typeof payload.tab_id === "number") lastPublishedTabId = payload.tab_id;
    const wire = {
      title: payload.title || "",
      artist: payload.artist || "",
      album: payload.album || "",
      playback_status: payload.state || payload.playback_status || "none",
      position_us: typeof payload.position === "number"
        ? Math.floor(payload.position * 1000000)
        : (typeof payload.position_us === "number" ? payload.position_us : 0),
      tab_id: typeof payload.tab_id === "number" ? payload.tab_id : null,
    };
    return dispatcher.request("mpris.publish", wire);
  }

  root.qdistroMpris = { update };
})(typeof self !== "undefined" ? self : globalThis);
