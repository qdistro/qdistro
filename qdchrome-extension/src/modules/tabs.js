// tabs module — 9b ops.
//
// Inbound (bridge → extension):
//   - tabs.list  → query all tabs, reply with id/title/url/status array
//   - tabs.open  → create a tab, reply with id
//   - tabs.close → remove tab(s), reply with ok
//
// Permission: `tabs` (declared in manifest). `activeTab` would not
// be enough — tabs.list across all windows requires `tabs`.
//
// @ts-check
(function (root) {
  "use strict";
  const api = root.qdistroApi;
  const dispatcher = root.qdistroDispatcher;

  /**
   * @param {chrome.tabs.Tab} t
   */
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
    };
  }

  function queryTabs(query) {
    return new Promise((resolve, reject) => {
      api.tabs.query(query || {}, (tabs) => {
        const err = api.runtime.lastError;
        if (err) return reject(new Error(err.message));
        resolve(tabs || []);
      });
    });
  }

  function createTab(props) {
    return new Promise((resolve, reject) => {
      api.tabs.create(props, (tab) => {
        const err = api.runtime.lastError;
        if (err) return reject(new Error(err.message));
        resolve(tab);
      });
    });
  }

  function removeTabs(ids) {
    return new Promise((resolve, reject) => {
      api.tabs.remove(ids, () => {
        const err = api.runtime.lastError;
        if (err) return reject(new Error(err.message));
        resolve(true);
      });
    });
  }

  // Deliver a one-shot message to a content script in `tabId` and
  // resolve with its reply. Used by the mpris module to forward an
  // inbound `mpris.control` op down to the originating tab's
  // mpris-content.js as `mpris.do_action`. Rejects (rather than
  // hangs) when the tab has no receiving content script — chrome
  // surfaces that as runtime.lastError ("Could not establish
  // connection. Receiving end does not exist.").
  function sendMessageToTab(tabId, message) {
    return new Promise((resolve, reject) => {
      try {
        api.tabs.sendMessage(tabId, message, (reply) => {
          const err = api.runtime.lastError;
          if (err) return reject(new Error(err.message));
          resolve(reply);
        });
      } catch (e) {
        reject(e instanceof Error ? e : new Error(String(e)));
      }
    });
  }

  dispatcher.register("tabs.list", async (_msg) => {
    const tabs = await queryTabs({});
    return { tabs: tabs.map(serialize) };
  });

  dispatcher.register("tabs.open", async (msg) => {
    const url = String(msg.url || "");
    if (!url) return { ok: false, error: "missing_url" };
    const tab = await createTab({ url, active: !!msg.active });
    return { tab: serialize(tab) };
  });

  dispatcher.register("tabs.close", async (msg) => {
    const ids = Array.isArray(msg.tab_ids) ? msg.tab_ids
      : (typeof msg.tab_id === "number" ? [msg.tab_id] : []);
    if (!ids.length) return { ok: false, error: "missing_tab_ids" };
    await removeTabs(ids);
    return { closed: ids };
  });

  // Expose for tests.
  root.qdistroTabs = {
    serialize, queryTabs, createTab, removeTabs, sendMessageToTab,
  };
})(typeof self !== "undefined" ? self : globalThis);
