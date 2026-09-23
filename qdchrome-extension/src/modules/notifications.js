// notifications module — 9e-3.
//
// Inbound only: the bridge calls `notifications.show` to surface a
// system notification via chrome.notifications.create. There is no
// outbound click/close forwarding — the bridge has no handler, and
// the chrome.notifications API only sees extension-owned
// notifications anyway (the page-level Notification API would need
// a content-script polyfill).
//
// @ts-check
(function (root) {
  "use strict";
  const api = root.qdistroApi;
  const dispatcher = root.qdistroDispatcher;

  function install() {
    // No outbound listeners. Kept as a no-op so callers don't have to
    // branch on the module shape.
  }

  // Inbound: bridge wants us to show a notification.
  dispatcher.register("notifications.show", async (msg) => {
    if (!api.notifications || !api.notifications.create) {
      return { ok: false, error: "notifications_unavailable" };
    }
    return await new Promise((resolve) => {
      api.notifications.create("", {
        type: "basic",
        iconUrl: msg.icon_url || "icons/icon-48.png",
        title: String(msg.title || "qdistro"),
        message: String(msg.message || ""),
      }, (id) => {
        resolve({ notification_id: id });
      });
    });
  });

  root.qdistroNotifications = { install };
})(typeof self !== "undefined" ? self : globalThis);
