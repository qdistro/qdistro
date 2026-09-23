// Module + origin gate — enforces the options-page toggles.
//
// The options page (options.js) persists two security controls into
// storage.local:
//   - `modules`: { tabs: bool, pwd: bool, ... } per-feature enable flags
//   - `origin_allowlist`: ["https://example.com", "https://*.corp", ...]
//
// Before this module those keys were dead: nothing read them, so
// unchecking "Cookies export" or setting an allowlist changed nothing.
// This gate is the single enforcement point. It caches the config at
// boot, re-reads it on storage.onChanged, and is consulted by:
//
//   - dispatcher.handleInbound  → reject bridge-initiated ops whose
//     owning module is disabled (e.g. the bridge asks for tabs.list
//     while Tabs is off).
//   - dispatcher.request        → reject extension-initiated outbound
//     ops whose owning module is disabled (e.g. a content script tries
//     to publish MPRIS while MPRIS is off).
//   - background runtime.onMessage → reject popup/content-script
//     req.kind entries whose module is disabled, and enforce the
//     origin allowlist on content-script-initiated ops (the URL is
//     derived in the background from sender.tab.url / the active tab,
//     never trusted from the page).
//
// FAIL-CLOSED defaults that DIFFER from "missing means off":
//   - A module with NO stored flag defaults to ENABLED — first-run /
//     never-saved config must not silently disable every feature, and
//     it matches the manifest's content_scripts being declared. Once
//     the user saves, the explicit booleans win (including `false`).
//   - The origin allowlist is CLOSED BY DEFAULT (opus HIGH J11): an
//     EMPTY / unset allowlist denies every page-initiated (content-
//     script) op, so a fresh or never-configured install does not ship
//     the credential/extraction surface open to every site the user
//     visits. To operate the extension the user lists the origins that
//     may drive the bridge, or enters a single `*` entry to explicitly
//     opt in to all origins (the old wide-open behaviour, now a
//     deliberate choice rather than the silent default). A NON-empty,
//     non-`*` allowlist restricts to the listed origins.
//     NOTE: this gate covers only content-script / page-initiated ops.
//     Bridge-initiated ops carry their own origin check at their call
//     sites (they too consult isOriginAllowed), and module on/off is a
//     separate gate — the allowlist never widens a disabled module.
//
// Infrastructure ops (qdistro.*: handshake, ping, heartbeat) are never
// gated — they're the transport, not a feature.
//
// @ts-check
(function (root) {
  "use strict";
  const api = root.qdistroApi;

  // Wire-op → module key. Covers both directions (inbound register and
  // outbound request) for every module across the chromium + firefox
  // source trees. An op with no entry here (e.g. qdistro.handshake) is
  // treated as infrastructure and never gated.
  const OP_MODULE = {
    "tabs.list": "tabs",
    "tabs.open": "tabs",
    "tabs.close": "tabs",
    "pwd.fill": "pwd",
    "pwd.fill_confirm": "pwd",
    "pwd.save": "pwd",
    "page.extract": "pageExtract",
    "page.extract.request": "pageExtract",
    "cookies.export": "cookies",
    "mpris.publish": "mpris",
    "mpris.control": "mpris",
    "downloads.notify": "downloads",
    "notifications.show": "notifications",
    "screenlock.inhibit": "screenlock",
    // NOTE: screenlock.release is deliberately NOT gated (codex #3). A
    // release must always be able to undo a prior inhibit — if the user
    // disables the screenlock module while a video is holding an
    // inhibit, gating the release would strand the lock-inhibit on and
    // keep the screen awake forever. Only the inhibit (acquire) side is
    // gated; the undo side always passes.
    "containers.list": "containers",
    "containers.create": "containers",
    "containers.remove": "containers",
  };

  // background runtime.onMessage req.kind → module key. `status` and
  // `ping` are infrastructure (popup connectivity) and not gated.
  // screenlock.report_release is intentionally absent — see the
  // screenlock.release note above (the undo path is never gated).
  const KIND_MODULE = {
    "cookies.export": "cookies",
    "pwd.request_fill": "pwd",
    "pwd.request_fill_confirm": "pwd",
    "pwd.request_save": "pwd",
    "mpris.report_update": "mpris",
    "screenlock.report_inhibit": "screenlock",
    "containers.list": "containers",
  };

  // Cached config. `null` modules means "no saved config" → every
  // module enabled (manifest defaults). An explicit object overrides
  // per key. `loaded` flips true once the first storage read returns.
  const state = {
    modules: null,           // {tabs:bool,...} or null
    allowlist: [],           // string[]; empty = deny all (closed by default)
    loaded: false,           // has the first storage.local.get returned?
  };

  // Readiness gate (codex finding #1): until the first storage read
  // completes we don't yet know whether the user disabled a module, so
  // enforcing synchronously here would fail OPEN during the brief
  // service-worker cold-start window. Async callers (the inbound
  // dispatcher + the background message handler) await ready() before
  // gating, so a saved `{module:false}` is honoured even on the very
  // first event that wakes the worker. After ready resolves, a missing
  // key legitimately means "default enabled".
  let _resolveReady;
  let ready = new Promise((res) => { _resolveReady = res; });
  function markReady() {
    state.loaded = true;
    if (_resolveReady) { _resolveReady(); _resolveReady = null; }
  }

  function applyConfig(cfg) {
    cfg = cfg || {};
    state.modules = (cfg.modules && typeof cfg.modules === "object")
      ? cfg.modules : null;
    state.allowlist = Array.isArray(cfg.origin_allowlist)
      ? cfg.origin_allowlist.filter((s) => typeof s === "string" && s)
      : [];
    markReady();
  }

  function load() {
    try {
      if (!api || !api.storage || !api.storage.local) { markReady(); return; }
      // Two API surfaces (codex #2): Firefox `browser.storage.local.get`
      // returns a Promise and ignores any callback; Chromium
      // `chrome.storage.local.get(keys, cb)` uses the callback and
      // returns undefined. We pass the callback for the Chromium path
      // but only apply from it when no thenable was returned, so the
      // Promise surface drives config exactly once. `applyConfig` is
      // idempotent regardless. The `usedPromise` flag (read inside the
      // callback, set only after get() returns a thenable) avoids
      // touching the not-yet-assigned `got` const — the callback can
      // fire synchronously, before get() returns, on the test fakes.
      let usedPromise = false;
      const got = api.storage.local.get(["modules", "origin_allowlist"], (cfg) => {
        if (!usedPromise) applyConfig(cfg);
      });
      if (got && typeof got.then === "function") {
        usedPromise = true;
        got.then(applyConfig, () => { markReady(); });
      }
    } catch (_) { markReady(); /* keep defaults: all modules enabled, allowlist closed (no origins) */ }
  }

  function watch() {
    try {
      if (api && api.storage && api.storage.onChanged) {
        api.storage.onChanged.addListener((changes, areaName) => {
          if (areaName && areaName !== "local") return;
          if (!changes) return;
          if ("modules" in changes) {
            const nv = changes.modules.newValue;
            state.modules = (nv && typeof nv === "object") ? nv : null;
          }
          if ("origin_allowlist" in changes) {
            const nv = changes.origin_allowlist.newValue;
            state.allowlist = Array.isArray(nv)
              ? nv.filter((s) => typeof s === "string" && s) : [];
          }
        });
      }
    } catch (_) { /* onChanged unavailable — config is then boot-static */ }
  }

  // A module is enabled unless storage carries an explicit `false`.
  function isModuleEnabled(mod) {
    if (!mod) return true; // infrastructure op
    const m = state.modules;
    if (!m) return true;   // no saved config → manifest defaults (on)
    return m[mod] !== false;
  }

  function opEnabled(op) {
    return isModuleEnabled(OP_MODULE[String(op || "")]);
  }

  function kindEnabled(kind) {
    return isModuleEnabled(KIND_MODULE[String(kind || "")]);
  }

  // Parse {scheme, host} out of a URL; null for non-http(s) / opaque
  // URLs (chrome://, about:blank, data:, blank tab).
  function parseUrl(url) {
    try {
      const u = new URL(String(url || ""));
      if (u.protocol !== "http:" && u.protocol !== "https:") return null;
      return { scheme: u.protocol.slice(0, -1), host: u.hostname.toLowerCase() };
    } catch (_) { return null; }
  }

  // Match an allowlist entry against a parsed {scheme, host}. Entries
  // may be a bare host ("example.com"), a full origin
  // ("https://example.com"), or a leading-wildcard subdomain pattern
  // ("https://*.corp" / "*.corp"). When the entry carries a scheme it
  // is enforced (codex finding #2): "https://bank.example" does NOT
  // allow http://bank.example. A bare-host entry matches either scheme.
  // Host match is exact unless the pattern is a `*.` wildcard, which
  // matches the suffix domain and any subdomain of it.
  function entryMatches(entry, parsed) {
    let pat = String(entry || "").trim().toLowerCase();
    if (!pat) return false;
    let scheme = null;
    const schemeIdx = pat.indexOf("://");
    if (schemeIdx !== -1) {
      scheme = pat.slice(0, schemeIdx);
      pat = pat.slice(schemeIdx + 3);
    }
    // Strip any path/query/port — match is host-based.
    pat = pat.split("/")[0].split(":")[0];
    if (!pat) return false;
    if (scheme && scheme !== parsed.scheme) return false;
    if (pat.startsWith("*.")) {
      const suffix = pat.slice(2);
      if (!suffix) return false;
      return parsed.host === suffix || parsed.host.endsWith("." + suffix);
    }
    return parsed.host === pat;
  }

  // CLOSED BY DEFAULT (opus HIGH J11). An empty / unset allowlist denies
  // every origin — a fresh install must not ship the page-initiated
  // credential/extraction surface open to every site. A single `*` entry
  // is the explicit opt-in to "all origins" (the pre-J11 wide-open
  // behaviour, now a deliberate user choice) and short-circuits to allow,
  // matching the old empty-means-all semantics including opaque URLs.
  // Otherwise the url must match a listed entry. A URL we can't parse
  // (chrome://, blank tab, opaque origin) is REJECTED unless `*` is set —
  // the allowlist is an explicit "only these origins" instruction.
  function isOriginAllowed(url) {
    const list = state.allowlist;
    if (!list.length) return false;                       // closed by default
    if (list.some((e) => String(e || "").trim() === "*")) return true; // explicit all-origins
    const parsed = parseUrl(url);
    if (!parsed) return false;
    for (const entry of list) {
      if (entryMatches(entry, parsed)) return true;
    }
    return false;
  }

  // Boot: read config and subscribe to changes.
  load();
  watch();

  root.qdistroGate = {
    isModuleEnabled,
    opEnabled,
    kindEnabled,
    isOriginAllowed,
    // Resolves once the first storage read has returned. Async gating
    // sites await this so a saved `{module:false}` is honoured even on
    // the worker's very first event (no cold-start fail-open window).
    ready: () => ready,
    isLoaded: () => state.loaded,
    opModule: (op) => OP_MODULE[String(op || "")] || null,
    kindModule: (kind) => KIND_MODULE[String(kind || "")] || null,
    // Test seams.
    _applyConfig: applyConfig,
    _state: state,
    _reload: load,
  };
})(typeof self !== "undefined" ? self : globalThis);
