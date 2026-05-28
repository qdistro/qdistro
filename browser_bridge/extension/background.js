// qdistro browser-bridge MV3 service-worker — Phase 9a/9b.
//
// Persistent native-messaging port with:
// - qdistro.handshake on connect (receives per-extension session secret)
// - Heartbeat ack (qdistro.heartbeat -> qdistro.heartbeat.ack)
// - Inbound request dispatch (tabs.list/open/close, page.extract.request,
//   cookies.export, containers.*, mpris/downloads/notifications/screenlock)
// - pwd.fill / pwd.save message forwarding from content script / popup
// - Intent-token minting (HMAC-SHA256 over "request_id|ts|op")
//
// Cross-browser: chrome.* with `browser` fallback so the same source
// runs under both Chromium MV3 and Firefox MV2.

const api = (typeof browser !== "undefined") ? browser : chrome;
const HOST = "qdistro";

// ---- session state --------------------------------------------------

let _port = null;           // persistent native-messaging port
let _sessionSecret = null;  // hex string from handshake, converted to Uint8Array for HMAC
let _tokenTtlS = 5.0;      // from handshake reply
let _requestSeq = 0;        // monotonic counter for intent-token request_ids
let _handshakeComplete = false;
let _pendingSaveOffer = null; // {url, username, password} from form submit

// Pending responses from the bridge keyed by request_id.  Used by
// popup / content-script message forwarding: they send a request,
// background.js forwards to bridge, parks a resolver, and fulfills it
// when the bridge replies.
//
// Note: the bridge does NOT echo request_id in its dispatch replies
// for extension-initiated ops (pwd.fill, pwd.save, ping, etc.).
// It only includes request_id for bridge-initiated inbound requests
// (tabs.list, heartbeat, etc.). For extension-initiated ops we
// fall back to matching by op using _pendingByOp.
const _pendingCallbacks = {};  // keyed by request_id (for bridge-initiated flows)
const _pendingByOp = {};       // keyed by op (FIFO queue per op for extension-initiated)

// ---- helpers --------------------------------------------------------

function nextRequestId() {
  _requestSeq++;
  const rand = Math.random().toString(36).slice(2, 10);
  return "ext-" + _requestSeq + "-" + rand;
}

// Convert hex string to Uint8Array
function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < hex.length; i += 2) {
    bytes[i / 2] = parseInt(hex.substr(i, 2), 16);
  }
  return bytes;
}

// HMAC-SHA256 using SubtleCrypto (available in service workers).
// Returns hex digest.
async function hmacSha256(keyBytes, message) {
  const key = await crypto.subtle.importKey(
    "raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key,
    new TextEncoder().encode(message));
  return Array.from(new Uint8Array(sig))
    .map(b => b.toString(16).padStart(2, "0")).join("");
}

// Mint an intent token for the given op. Returns a promise resolving
// to {request_id, ts, op, hmac}.
async function mintIntentToken(op) {
  if (!_sessionSecret) {
    throw new Error("no session secret — handshake not complete");
  }
  const request_id = nextRequestId();
  const ts = Date.now() / 1000;  // seconds, matching Python time.time()
  const canonical = request_id + "|" + ts + "|" + op;
  const mac = await hmacSha256(_sessionSecret, canonical);
  return { request_id, ts, op, hmac: mac };
}

// Send a message to the bridge and return a promise for the reply.
// The bridge's dispatch() sets body["op"] on replies but does NOT
// echo request_id for extension-initiated ops. We track pending
// requests both by request_id (in case the bridge adds one) and by
// op (FIFO queue) so that when the reply arrives we can match it.
function bridgeRequest(msg) {
  return new Promise((resolve, reject) => {
    if (!_port) {
      reject(new Error("bridge port not connected"));
      return;
    }
    const op = msg.op || "";
    const rid = msg.request_id || nextRequestId();
    msg.request_id = rid;
    const entry = { resolve, reject, ts: Date.now(), op, rid };

    // Track by request_id (for bridge-initiated replies that echo it)
    _pendingCallbacks[rid] = entry;
    // Also track by op in a FIFO queue (for extension-initiated replies)
    if (!_pendingByOp[op]) _pendingByOp[op] = [];
    _pendingByOp[op].push(entry);

    try {
      _port.postMessage(msg);
    } catch (e) {
      delete _pendingCallbacks[rid];
      _removePendingByOp(op, rid);
      reject(e);
    }
    // Timeout after 70 seconds (bridge's inbound timeout is 75s).
    setTimeout(() => {
      if (_pendingCallbacks[rid]) {
        delete _pendingCallbacks[rid];
        _removePendingByOp(op, rid);
        resolve({ ok: false, error: "extension_timeout" });
      }
    }, 70000);
  });
}

function _removePendingByOp(op, rid) {
  const q = _pendingByOp[op];
  if (!q) return;
  const idx = q.findIndex(e => e.rid === rid);
  if (idx >= 0) q.splice(idx, 1);
  if (!q.length) delete _pendingByOp[op];
}

// ---- inbound dispatch (bridge -> extension) -------------------------
// The bridge pushes requests like {op: "tabs.list", request_id: "r3-..."}
// down the pipe. We execute the browser API call and reply with
// {op: "<op>.reply", request_id, ...result}.

async function handleInboundRequest(msg) {
  const op = msg.op;
  const rid = msg.request_id || "";
  let result;

  try {
    switch (op) {
      case "tabs.list":
        result = await handleTabsList(msg);
        break;
      case "tabs.open":
        result = await handleTabsOpen(msg);
        break;
      case "tabs.close":
        result = await handleTabsClose(msg);
        break;
      case "page.extract.request":
        result = await handlePageExtractRequest(msg);
        break;
      // cookies.export is NOT handled inbound — it must be extension-
      // initiated with intent-token validation through the bridge's
      // own _handle_cookies_export handler. The inbound D-Bus surface
      // must not bypass intent tokens and audit logging.
      case "containers.list":
        result = await handleContainersList(msg);
        break;
      case "containers.create":
        result = await handleContainersCreate(msg);
        break;
      case "containers.remove":
        result = await handleContainersRemove(msg);
        break;
      default:
        result = { ok: false, error: "unknown_inbound_op", op: op };
        break;
    }
  } catch (e) {
    result = { ok: false, error: "handler_error", detail: String(e).slice(0, 200) };
  }

  // Send reply back to bridge
  if (_port && rid) {
    try {
      _port.postMessage({
        op: op + ".reply",
        request_id: rid,
        ...result
      });
    } catch (e) {
      // Port closed — nothing we can do.
    }
  }
}

// ---- tabs handlers --------------------------------------------------

async function handleTabsList(_msg) {
  const tabs = await api.tabs.query({});
  return {
    ok: true,
    tabs: tabs.map(t => ({
      id: t.id,
      url: t.url || "",
      title: t.title || "",
      active: !!t.active,
      window_id: t.windowId,
      status: t.status || "complete",
      pinned: !!t.pinned,
      incognito: !!t.incognito,
    }))
  };
}

async function handleTabsOpen(msg) {
  const url = msg.url || "about:blank";
  const active = msg.active !== false;
  const opts = { url, active };
  // Firefox containers: open in a specific container if requested
  const csid = msg.cookie_store_id || msg.cookieStoreId;
  if (csid) opts.cookieStoreId = csid;
  const tab = await api.tabs.create(opts);
  return { ok: true, id: tab.id, url: tab.url || url };
}

async function handleTabsClose(msg) {
  const tabId = msg.tab_id || msg.tabId;
  if (typeof tabId !== "number") {
    return { ok: false, error: "missing_tab_id" };
  }
  await api.tabs.remove(tabId);
  return { ok: true, tab_id: tabId };
}

// ---- page.extract.request handler -----------------------------------
// Bridge sends {op: "page.extract.request", tab_id, mode, selector?}
// Extension extracts content from the specified tab.
// Wire contract: see todo/browser/02-page-extract-request-usage.md

const PAGE_EXTRACT_MODES = new Set([
  "selection", "visible_text", "full_text", "outer_html", "by_selector", "title"
]);
const PAGE_EXTRACT_SIZE_CAP = 256 * 1024;  // 256 KB

async function handlePageExtractRequest(msg) {
  const mode = msg.mode || "visible_text";
  if (!PAGE_EXTRACT_MODES.has(mode)) {
    return { ok: false, error: "unknown_mode", mode };
  }
  const selector = msg.selector || "";
  if (mode === "by_selector" && !selector) {
    return { ok: false, error: "missing_selector" };
  }
  // tab_id is required per the documented contract
  const requestedTabId = msg.tab_id;
  if (typeof requestedTabId !== "number") {
    return { ok: false, error: "missing_tab_id" };
  }
  let tab;
  try {
    tab = await api.tabs.get(requestedTabId);
  } catch (e) {
    return { ok: false, error: "executeScript_failed", detail: "tab not found: " + String(e).slice(0, 150) };
  }

  let content = "";
  let matched = undefined;  // only set for by_selector mode
  try {
    if (api.scripting && api.scripting.executeScript) {
      // MV3 path
      const results = await api.scripting.executeScript({
        target: { tabId: tab.id },
        func: function(m, s) {
          if (m === "selection") return window.getSelection().toString();
          if (m === "title") return document.title;
          if (m === "visible_text") return document.body ? document.body.innerText : "";
          if (m === "full_text") return document.documentElement ? document.documentElement.textContent : "";
          if (m === "outer_html") return document.documentElement ? document.documentElement.outerHTML : "";
          if (m === "by_selector" && s) {
            try {
              var el = document.querySelector(s);
              return el ? { text: el.innerText, matched: true } : { text: "", matched: false };
            } catch (e) {
              return { error: "bad_selector", detail: e.message };
            }
          }
          return "";
        },
        args: [mode, selector]
      });
      if (results && results[0]) {
        const r = results[0].result;
        if (r && typeof r === "object" && r.error) {
          return { ok: false, error: r.error, detail: r.detail || "" };
        }
        if (r && typeof r === "object" && "matched" in r) {
          content = r.text || "";
          matched = !!r.matched;
        } else {
          content = r || "";
        }
      }
    } else {
      // MV2 / Firefox fallback
      const extractFn = `(function() {
        var mode = ${JSON.stringify(mode)};
        var selector = ${JSON.stringify(selector)};
        if (mode === "selection") return window.getSelection().toString();
        if (mode === "title") return document.title;
        if (mode === "visible_text") return document.body ? document.body.innerText : "";
        if (mode === "full_text") return document.documentElement ? document.documentElement.textContent : "";
        if (mode === "outer_html") return document.documentElement ? document.documentElement.outerHTML : "";
        if (mode === "by_selector" && selector) {
          try {
            var el = document.querySelector(selector);
            return JSON.stringify(el ? { text: el.innerText, matched: true } : { text: "", matched: false });
          } catch (e) {
            return JSON.stringify({ error: "bad_selector", detail: e.message });
          }
        }
        return "";
      })()`;
      const results = await api.tabs.executeScript(tab.id, { code: extractFn });
      let r = (results && results[0]) || "";
      if (mode === "by_selector" && typeof r === "string") {
        try {
          const parsed = JSON.parse(r);
          if (parsed.error) return { ok: false, error: parsed.error, detail: parsed.detail || "" };
          content = parsed.text || "";
          matched = !!parsed.matched;
        } catch (_e) {
          content = r;
        }
      } else {
        content = r || "";
      }
    }
  } catch (e) {
    return { ok: false, error: "executeScript_failed", detail: String(e).slice(0, 200) };
  }
  if (content === undefined || content === null) {
    return { ok: false, error: "capture_returned_empty" };
  }
  content = String(content);
  // Enforce 256 KB size cap
  let truncated = false;
  if (content.length > PAGE_EXTRACT_SIZE_CAP) {
    content = content.slice(0, PAGE_EXTRACT_SIZE_CAP);
    truncated = true;
  }
  const reply = {
    ok: true,
    mode,
    url: tab.url || "",
    title: tab.title || "",
    content,
    truncated,
  };
  if (mode === "by_selector") {
    reply.matched = matched !== undefined ? matched : false;
  }
  return reply;
}

// ---- cookies.export inbound handler ---------------------------------
// Bridge requests cookie export for a domain from extension side.
// The domain may be a bare domain like "example.com" or a full URL.
// The cookies.getAll API requires either `url` or `domain` params.

async function handleCookiesExportInbound(msg) {
  const rawDomain = msg.domain || msg.url || "";
  if (!rawDomain) {
    return { ok: false, error: "missing_domain" };
  }
  if (!api.cookies) {
    return { ok: false, error: "cookies_api_unavailable" };
  }
  try {
    // Build the query: if it looks like a URL use `url`, otherwise
    // use `domain` to handle bare domain names like "example.com".
    const query = {};
    if (/^https?:\/\//.test(rawDomain)) {
      query.url = rawDomain;
    } else {
      query.domain = rawDomain;
    }
    // Support Firefox container-scoped export
    const storeId = msg.cookie_store_id || msg.storeId || msg.cookieStoreId;
    if (storeId) query.storeId = storeId;

    const cookies = await api.cookies.getAll(query);
    return {
      ok: true,
      cookies: cookies.map(c => ({
        name: c.name,
        value: c.value,
        domain: c.domain,
        path: c.path,
        secure: c.secure,
        httpOnly: c.httpOnly,
        expirationDate: c.expirationDate || null,
        storeId: c.storeId || null,
      }))
    };
  } catch (e) {
    return { ok: false, error: "cookies_get_failed", detail: String(e).slice(0, 200) };
  }
}

// ---- Firefox containers (contextualIdentities) ----------------------

async function handleContainersList(_msg) {
  if (!api.contextualIdentities) {
    return { ok: false, error: "contextualIdentities_unavailable", containers: [] };
  }
  try {
    const ids = await api.contextualIdentities.query({});
    return {
      ok: true,
      containers: ids.map(c => ({
        cookie_store_id: c.cookieStoreId,
        name: c.name,
        color: c.color,
        color_code: c.colorCode || "",
        icon: c.icon,
        icon_url: c.iconUrl || "",
      }))
    };
  } catch (e) {
    return { ok: false, error: "containers_list_failed", detail: String(e).slice(0, 200) };
  }
}

async function handleContainersCreate(msg) {
  if (!api.contextualIdentities) {
    return { ok: false, error: "contextualIdentities_unavailable" };
  }
  try {
    const c = await api.contextualIdentities.create({
      name: msg.name || "qdistro",
      color: msg.color || "blue",
      icon: msg.icon || "fingerprint",
    });
    return {
      ok: true,
      container: {
        cookie_store_id: c.cookieStoreId,
        name: c.name,
        color: c.color,
        color_code: c.colorCode || "",
        icon: c.icon,
        icon_url: c.iconUrl || "",
      }
    };
  } catch (e) {
    return { ok: false, error: "containers_create_failed", detail: String(e).slice(0, 200) };
  }
}

async function handleContainersRemove(msg) {
  if (!api.contextualIdentities) {
    return { ok: false, error: "contextualIdentities_unavailable" };
  }
  // Accept both snake_case (protocol) and camelCase (JS native) field names
  const csid = msg.cookie_store_id || msg.cookieStoreId;
  if (!csid) {
    return { ok: false, error: "missing_cookie_store_id" };
  }
  try {
    const c = await api.contextualIdentities.remove(csid);
    return {
      ok: true,
      container: {
        cookie_store_id: c.cookieStoreId,
        name: c.name,
        color: c.color,
        color_code: c.colorCode || "",
        icon: c.icon,
        icon_url: c.iconUrl || "",
      }
    };
  } catch (e) {
    return { ok: false, error: "containers_remove_failed", detail: String(e).slice(0, 200) };
  }
}

// ---- persistent port management -------------------------------------

function connectBridge() {
  if (_port) {
    try { _port.disconnect(); } catch (_e) { /* ignore */ }
  }
  _port = null;
  _handshakeComplete = false;
  _sessionSecret = null;

  try {
    _port = api.runtime.connectNative(HOST);
  } catch (e) {
    console.error("qdistro: connectNative failed:", e);
    // Retry after a delay
    setTimeout(connectBridge, 5000);
    return;
  }

  _port.onMessage.addListener(onBridgeMessage);
  _port.onDisconnect.addListener(onBridgeDisconnect);

  // Initiate handshake immediately
  _port.postMessage({ op: "qdistro.handshake" });
}

function onBridgeDisconnect() {
  const err = api.runtime.lastError;
  console.warn("qdistro: bridge disconnected",
    err ? err.message : "(no error)");
  _port = null;
  _handshakeComplete = false;
  _sessionSecret = null;
  // Clear all pending callbacks with an error
  for (const rid of Object.keys(_pendingCallbacks)) {
    const cb = _pendingCallbacks[rid];
    delete _pendingCallbacks[rid];
    if (cb && cb.resolve) {
      cb.resolve({ ok: false, error: "bridge_disconnected" });
    }
  }
  for (const op of Object.keys(_pendingByOp)) {
    delete _pendingByOp[op];
  }
  // Attempt reconnect after a delay. MV3 service worker may be
  // suspended — the alarm or next event will re-trigger connect.
  setTimeout(connectBridge, 3000);
}

function onBridgeMessage(msg) {
  if (!msg || typeof msg !== "object") return;

  const op = msg.op || "";

  // 1. Heartbeat: reply immediately
  if (op === "qdistro.heartbeat") {
    if (_port) {
      try {
        _port.postMessage({
          op: "qdistro.heartbeat.ack",
          request_id: msg.request_id || "",
        });
      } catch (_e) { /* port closed */ }
    }
    return;
  }

  // 2. Handshake reply: store session secret
  if (op === "qdistro.handshake" && msg.session_secret_hex) {
    _sessionSecret = hexToBytes(msg.session_secret_hex);
    _tokenTtlS = msg.token_ttl_s || 5.0;
    _handshakeComplete = true;
    console.log("qdistro: handshake complete, extension_id:", msg.extension_id || "(none)");
    return;
  }

  // 3. Response to a pending request — match by request_id first
  const rid = msg.request_id || "";
  if (rid && _pendingCallbacks[rid]) {
    const cb = _pendingCallbacks[rid];
    delete _pendingCallbacks[rid];
    _removePendingByOp(cb.op, rid);
    if (cb.resolve) cb.resolve(msg);
    return;
  }

  // 4. Response to a pending request — match by op (FIFO).
  //    The bridge's dispatch() does NOT echo request_id for
  //    extension-initiated ops, so we fall back to op-based matching.
  if (op && _pendingByOp[op] && _pendingByOp[op].length) {
    const entry = _pendingByOp[op].shift();
    if (!_pendingByOp[op].length) delete _pendingByOp[op];
    delete _pendingCallbacks[entry.rid];
    if (entry.resolve) entry.resolve(msg);
    return;
  }

  // 5. Inbound request from bridge (daemon-initiated)
  //    These have an op without ".reply" suffix and a request_id that
  //    doesn't match any pending callback — the bridge wants us to
  //    execute a browser API call and reply.
  if (rid && !op.endsWith(".reply")) {
    handleInboundRequest(msg);
    return;
  }

  // 6. Unmatched message — log for debugging
  console.warn("qdistro: unmatched bridge message", op, rid);
}

// ---- message listener (from popup / content scripts) ----------------
// The popup and content scripts communicate with the background via
// chrome.runtime.sendMessage / onMessage.

api.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;
  const action = msg.action;

  if (action === "pwd.fill") {
    handlePwdFill(msg, sender).then(sendResponse);
    return true;  // async response
  }

  if (action === "pwd.fill_confirm") {
    handlePwdFillConfirm(msg, sender).then(sendResponse);
    return true;
  }

  if (action === "pwd.save") {
    handlePwdSave(msg, sender).then(sendResponse);
    return true;
  }

  if (action === "cookies.export") {
    handleCookiesExport(msg, sender).then(sendResponse);
    return true;
  }

  if (action === "pwd.save_offer") {
    // Content script detected a form submission with new credentials.
    // Store the offer so the popup can prompt the user.
    _pendingSaveOffer = {
      url: msg.url || "",
      username: msg.username || "",
      password: msg.password || "",
      ts: Date.now(),
    };
    // Set badge to signal a pending save (Chromium MV3 / Firefox)
    try {
      const badgeApi = api.action || api.browserAction;
      if (badgeApi && badgeApi.setBadgeText) {
        badgeApi.setBadgeText({ text: "!" });
        badgeApi.setBadgeBackgroundColor({ color: "#1a73e8" });
      }
    } catch (_e) { /* ignore */ }
    sendResponse({ ok: true });
    return false;
  }

  if (action === "get_pending_save") {
    // Popup retrieves the pending save offer
    sendResponse({ offer: _pendingSaveOffer });
    return false;
  }

  if (action === "dismiss_pending_save") {
    _pendingSaveOffer = null;
    try {
      const badgeApi = api.action || api.browserAction;
      if (badgeApi && badgeApi.setBadgeText) {
        badgeApi.setBadgeText({ text: "" });
      }
    } catch (_e) { /* ignore */ }
    sendResponse({ ok: true });
    return false;
  }

  if (action === "get_status") {
    sendResponse({
      connected: !!_port,
      handshakeComplete: _handshakeComplete,
      pendingSave: !!_pendingSaveOffer,
    });
    return false;
  }

  if (action === "ping") {
    handlePing().then(sendResponse);
    return true;
  }

  return false;
});

// ---- pwd.fill handler -----------------------------------------------

async function handlePwdFill(msg, _sender) {
  if (!_handshakeComplete) {
    return { ok: false, error: "handshake_not_complete" };
  }
  try {
    const token = await mintIntentToken("pwd.fill");
    return await bridgeRequest({
      op: "pwd.fill",
      url: msg.url || "",
      username: msg.username || "",
      intent_token: token,
    });
  } catch (e) {
    return { ok: false, error: "pwd_fill_failed", detail: String(e).slice(0, 200) };
  }
}

// ---- pwd.fill_confirm handler ---------------------------------------

async function handlePwdFillConfirm(msg, _sender) {
  if (!_handshakeComplete) {
    return { ok: false, error: "handshake_not_complete" };
  }
  try {
    const token = await mintIntentToken("pwd.fill_confirm");
    return await bridgeRequest({
      op: "pwd.fill_confirm",
      url: msg.url || "",
      username: msg.username || "",
      fill_token: msg.fill_token || "",
      intent_token: token,
    });
  } catch (e) {
    return { ok: false, error: "pwd_fill_confirm_failed", detail: String(e).slice(0, 200) };
  }
}

// ---- pwd.save handler -----------------------------------------------

async function handlePwdSave(msg, _sender) {
  if (!_handshakeComplete) {
    return { ok: false, error: "handshake_not_complete" };
  }
  try {
    const token = await mintIntentToken("pwd.save");
    return await bridgeRequest({
      op: "pwd.save",
      url: msg.url || "",
      username: msg.username || "",
      password: msg.password || "",
      intent_token: token,
    });
  } catch (e) {
    return { ok: false, error: "pwd_save_failed", detail: String(e).slice(0, 200) };
  }
}

// ---- cookies.export handler ----------------------------------------

function isHttpUrl(url) {
  return typeof url === "string" &&
    (url.startsWith("http://") || url.startsWith("https://"));
}

function normalizeCookie(cookie) {
  return {
    name: cookie.name || "",
    value: cookie.value || "",
    domain: cookie.domain || "",
    path: cookie.path || "",
    secure: !!cookie.secure,
    httpOnly: !!cookie.httpOnly,
    sameSite: cookie.sameSite || "unspecified",
    session: !!cookie.session,
    expirationDate: cookie.expirationDate || null,
    storeId: cookie.storeId || null,
  };
}

async function handleCookiesExport(_msg, _sender) {
  if (!_handshakeComplete) {
    return { ok: false, error: "handshake_not_complete" };
  }
  if (!api.cookies || !api.cookies.getAll) {
    return { ok: false, error: "cookies_api_unavailable" };
  }

  if (_sender && _sender.tab) {
    return { ok: false, error: "popup_required" };
  }
  const popupUrl = api.runtime.getURL("popup.html");
  if (!_sender || _sender.url !== popupUrl) {
    return { ok: false, error: "popup_required" };
  }

  const tabs = await api.tabs.query({ active: true, currentWindow: true });
  const tab = tabs && tabs.length ? tabs[0] : null;
  const url = tab ? (tab.url || "") : "";
  if (!isHttpUrl(url)) {
    return { ok: false, error: "unsupported_url" };
  }

  try {
    const host = (new URL(url)).hostname;
    const query = { domain: host };
    const cookieStoreId = tab.cookieStoreId || "";
    if (cookieStoreId) query.storeId = cookieStoreId;

    const cookies = await api.cookies.getAll(query);
    const token = await mintIntentToken("cookies.export");
    return await bridgeRequest({
      op: "cookies.export",
      url,
      domain: host,
      cookie_store_id: cookieStoreId,
      cookies: cookies.map(normalizeCookie),
      intent_token: token,
    });
  } catch (e) {
    return { ok: false, error: "cookies_export_failed", detail: String(e).slice(0, 200) };
  }
}

// ---- ping handler (popup "test connection" button) ------------------

async function handlePing() {
  try {
    return await bridgeRequest({
      op: "qdistro.ping",
      echo: String(Date.now()),
    });
  } catch (e) {
    return { ok: false, error: "ping_failed", detail: String(e).slice(0, 200) };
  }
}

// ---- startup --------------------------------------------------------

// Connect immediately on service worker load. On MV3, the service
// worker is killed after 30s of idle — the heartbeat at 25s intervals
// keeps it alive. If the SW is killed and restarted (e.g. by a popup
// open or content-script message), connectBridge() re-establishes.
connectBridge();

// MV3 install / activate handlers
self.addEventListener("install", () => {
  // No-op — connectBridge() runs at top level on every SW start.
});
