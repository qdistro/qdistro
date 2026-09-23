// tabs module.
//
// Inbound (bridge → extension):
//   - tabs.list  → query all tabs, reply with id/title/url/status array
//   - tabs.open  → create a tab, reply with id; honors `cookie_store_id`
//                  to pin the new tab to a Firefox container.
//   - tabs.close → remove tab(s), reply with ok
//
// Firefox tabs.create supports `cookieStoreId` which Chromium ignores;
// we accept the same field on the wire so the daemon can drive
// container-scoped tabs from policy without per-browser branching.
//
// Permission: `tabs` (manifest). `activeTab` alone wouldn't cover
// tabs.list across windows.
//
// @ts-check
(function (root) {
  "use strict";
  const api = root.qdistroApi;
  const dispatcher = root.qdistroDispatcher;

  function serialize(t) {
    return {
      id: t.id,
      window_id: t.windowId,
      index: t.index,
      url: t.url || t.pendingUrl || "",
      title: t.title || "",
      active: !!t.active,
      pinned: !!t.pinned,
      audible: !!t.audible,
      muted: !!(t.mutedInfo && t.mutedInfo.muted),
      status: t.status || "complete",
      cookie_store_id: t.cookieStoreId || null,
    };
  }

  async function queryTabs(query) {
    return await api.tabs.query(query || {});
  }

  async function createTab(props) {
    return await api.tabs.create(props);
  }

  async function removeTabs(ids) {
    await api.tabs.remove(ids);
    return true;
  }

  // Deliver a one-shot message to a content script in `tabId` and
  // resolve with its reply. Used by the mpris module to forward an
  // inbound `mpris.control` op down to the originating tab's
  // mpris-content.js as `mpris.do_action`. Firefox's Promise-based
  // tabs.sendMessage rejects when no content script is listening, so
  // the caller sees a thrown error rather than a silent hang.
  async function sendMessageToTab(tabId, message) {
    return await api.tabs.sendMessage(tabId, message);
  }

  dispatcher.register("tabs.list", async (_msg) => {
    const tabs = await queryTabs({});
    return { tabs: tabs.map(serialize) };
  });

  dispatcher.register("tabs.open", async (msg) => {
    const url = String(msg.url || "");
    if (!url) return { ok: false, error: "missing_url" };
    const props = { url, active: !!msg.active };
    if (msg.cookie_store_id) props.cookieStoreId = String(msg.cookie_store_id);
    const tab = await createTab(props);
    return { tab: serialize(tab) };
  });

  dispatcher.register("tabs.close", async (msg) => {
    const ids = Array.isArray(msg.tab_ids) ? msg.tab_ids
      : (typeof msg.tab_id === "number" ? [msg.tab_id] : []);
    if (!ids.length) return { ok: false, error: "missing_tab_ids" };
    await removeTabs(ids);
    return { closed: ids };
  });

  root.qdistroTabs = {
    serialize, queryTabs, createTab, removeTabs, sendMessageToTab,
  };
})(typeof self !== "undefined" ? self : globalThis);
