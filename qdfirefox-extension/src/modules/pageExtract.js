// page.extract module.
//
// Context-menu-driven. User selects text → right-click → "Send to…" →
// we capture {url, title, selected_text} and forward to bridge as
// `page.extract` with an intent token.
//
// Content-script injection on demand via browser.scripting.executeScript
// (MV3). Avoids the always-on permission cost of a static content
// script.
//
// @ts-check
(function (root) {
  "use strict";
  const api = root.qdistroApi;
  const dispatcher = root.qdistroDispatcher;

  async function executeInTab(tabId, func, args) {
    // Firefox MV3 exposes browser.scripting.executeScript with the
    // same shape Chromium uses. Return value is the first frame's
    // result.
    const results = await api.scripting.executeScript({
      target: { tabId },
      func,
      args: args || [],
    });
    return results && results[0] && results[0].result;
  }

  // Resolve a tab's authoritative URL for the origin allowlist. Works
  // with both the Promise (Firefox) and callback shapes of tabs.get.
  // Returns "" when the tab can't be resolved.
  function tabUrl(tabId) {
    return new Promise((resolve) => {
      try {
        if (!api.tabs || !api.tabs.get) { resolve(""); return; }
        const got = api.tabs.get(tabId, (t) => {
          if (api.runtime && api.runtime.lastError) { resolve(""); return; }
          resolve((t && t.url) || "");
        });
        if (got && typeof got.then === "function") {
          got.then((t) => resolve((t && t.url) || ""), () => resolve(""));
        }
      } catch (_) { resolve(""); }
    });
  }

  function captureSelectionFn() {
    const sel = (window.getSelection && window.getSelection().toString()) || "";
    return {
      selected_text: sel,
      url: location.href,
      title: document.title,
    };
  }

  // Runs in the page context. Mode-driven content snapshot for
  // bridge-initiated page.extract.request.
  function captureByModeFn(mode, selector) {
    const cap = {
      mode,
      url: location.href,
      title: document.title,
      content: "",
      truncated: false,
    };
    const MAX = 256 * 1024; // 256KB cap so we don't drown the bridge.
    function trim(s) {
      if (typeof s !== "string") return "";
      if (s.length > MAX) {
        cap.truncated = true;
        return s.slice(0, MAX);
      }
      return s;
    }
    switch (mode) {
      case "selection":
        cap.content = trim(
          (window.getSelection && window.getSelection().toString()) || "");
        break;
      case "visible_text":
        cap.content = trim(document.body ? document.body.innerText || "" : "");
        break;
      case "full_text":
        cap.content = trim(document.documentElement
          ? document.documentElement.textContent || "" : "");
        break;
      case "outer_html":
        cap.content = trim(document.documentElement
          ? document.documentElement.outerHTML || "" : "");
        break;
      case "by_selector": {
        const sel = String(selector || "");
        if (!sel) {
          cap.error = "missing_selector";
          return cap;
        }
        try {
          const el = document.querySelector(sel);
          cap.content = trim(el ? el.innerText || el.textContent || "" : "");
          cap.matched = !!el;
        } catch (e) {
          cap.error = "bad_selector";
        }
        break;
      }
      case "title":
        cap.content = cap.title;
        break;
      default:
        cap.error = "unknown_mode";
    }
    return cap;
  }

  async function capture(tabId) {
    const captured = await executeInTab(tabId, captureSelectionFn);
    return captured || { selected_text: "", url: "", title: "" };
  }

  async function extract(tabId, destination, intentToken) {
    const captured = await capture(tabId);
    return await dispatcher.request("page.extract", {
      ...captured,
      destination: destination || null,
      intent_token: intentToken || null,
    });
  }

  function installContextMenu() {
    if (!api.contextMenus) return;
    try {
      api.contextMenus.create({
        id: "qdistro-share-to",
        title: "Send to qdistro…",
        contexts: ["selection", "page", "link"],
      });
    } catch (_) { /* idempotent: already installed */ }

    api.contextMenus.onClicked.addListener(async (info, tab) => {
      if (info.menuItemId !== "qdistro-share-to") return;
      if (!tab || tab.id == null) return;
      // Origin allowlist (options page): only extract from listed
      // origins. tab.url is the authoritative frame URL from the
      // browser. Await the gate's first storage read so a cold-start
      // context-menu click can't slip past the allowlist while it's
      // still loading (codex #1 follow-up). Module on/off is gated
      // downstream in the dispatcher.
      // FAIL CLOSED if the gate module is absent (never loaded, or it
      // threw before exporting): an extension with no working origin
      // gate must not run privileged page-initiated ops at all (J11).
      if (!root.qdistroGate) {
        console.warn(
          "[qdistro/pageExtract] extract refused: the origin gate did not "
          + "load; refusing rather than running ungated.");
        return;
      }
      {
        if (root.qdistroGate.ready && !root.qdistroGate.isLoaded()) {
          await root.qdistroGate.ready();
        }
        if (!root.qdistroGate.isOriginAllowed(tab.url || "")) {
          // The allowlist is closed by default (J11). A user-gesture
          // extract that is refused would otherwise be a silent no-op —
          // log where to fix it so the feature doesn't look broken.
          console.warn(
            "[qdistro/pageExtract] extract refused: origin not on the "
            + "allowlist. Add the origin (or `*`) on the extension's "
            + "options page to enable page extraction here.");
          return;
        }
      }
      try {
        const intentToken = root.qdistroIntent
          ? await root.qdistroIntent.mint("page.extract", 5000)
          : null;
        await extract(tab.id, info.selectionText ? "selection" : "page", intentToken);
      } catch (e) {
        console.warn("[qdistro/pageExtract] failed", e);
      }
    });
  }

  // Bridge-initiated extraction. Bridge sends:
  //   {op: "page.extract.request", request_id, tab_id, mode, selector?}
  // Modes:
  //   selection / visible_text / full_text / outer_html / by_selector / title
  // No intent token here — the bridge gates incoming D-Bus callers
  // via _identity_gate (peer selinux context). Symmetric with the
  // tabs.list / tabs.open / tabs.close bridge → ext path.
  dispatcher.register("page.extract.request", async (msg) => {
    const tabId = msg.tab_id;
    const mode = String(msg.mode || "visible_text");
    if (typeof tabId !== "number") {
      return { ok: false, error: "missing_tab_id" };
    }
    // Origin allowlist (options page): resolve the target tab's real
    // URL and refuse extraction from an off-list page, even though the
    // request comes from the trusted bridge (codex finding #4). Await
    // the gate's first read so a cold-start request can't slip past the
    // allowlist while it loads. Module on/off is gated in the dispatcher.
    // FAIL CLOSED when the gate is absent — see the context-menu path.
    if (!root.qdistroGate) {
      return { ok: false, error: "origin_not_allowed" };
    }
    {
      if (root.qdistroGate.ready && !root.qdistroGate.isLoaded()) {
        await root.qdistroGate.ready();
      }
      const url = await tabUrl(tabId);
      if (!root.qdistroGate.isOriginAllowed(url)) {
        return { ok: false, error: "origin_not_allowed" };
      }
    }
    try {
      const captured = await executeInTab(
        tabId, captureByModeFn, [mode, msg.selector || ""]);
      if (!captured) {
        return { ok: false, error: "capture_returned_empty" };
      }
      if (captured.error) {
        return { ok: false, error: captured.error, mode, url: captured.url };
      }
      return {
        mode: captured.mode,
        url: captured.url || "",
        title: captured.title || "",
        content: captured.content || "",
        truncated: !!captured.truncated,
        ...(typeof captured.matched === "boolean"
          ? { matched: captured.matched } : {}),
      };
    } catch (e) {
      return { ok: false, error: "executeScript_failed",
               detail: String(e.message || e).slice(0, 200) };
    }
  });

  async function extractByMode(tabId, mode, selector) {
    const captured = await executeInTab(tabId, captureByModeFn,
      [mode, selector || ""]);
    return captured || { content: "", url: "", title: "", mode };
  }

  root.qdistroPageExtract = {
    extract, capture, installContextMenu, extractByMode,
  };
})(typeof self !== "undefined" ? self : globalThis);
