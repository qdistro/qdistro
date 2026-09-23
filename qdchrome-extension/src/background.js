// qdistro browser-bridge background — Chromium MV3 service worker.
// Boot sequence:
//
//   1. importScripts: load api shim, port manager, dispatcher,
//      intent, then every per-feature module under modules/.
//      Each module registers its handlers on import.
//   2. Open the persistent native-messaging port. Reconnect-with-
//      backoff and the 25s heartbeat watchdog run inside port.js.
//   3. Install context menus and event listeners (downloads,
//      notifications) on the worker scope.
//   4. Handle popup → background runtime.sendMessage so the popup
//      can drive the persistent port without opening its own
//      connectNative (one host per browser session per spec/14).
//
// importScripts is the MV3-correct way to compose a service worker.
// The call is wrapped in try/catch so environments without it (the
// vitest harness, which pre-loads the module globals directly) skip
// the import and reuse the already-defined globals. This repo is
// Chromium-only; the Firefox extensions live in their own repos — the
// maintained ../qdfirefox-extension and the legacy bundled tree (see
// ../doc/browser.md "Firefox extension artifacts").
//
// @ts-check

try {
  // MV3 path. The build script puts these files alongside.
  // eslint-disable-next-line no-undef
  importScripts(
    "src/api.js",
    "src/port.js",
    "src/dispatcher.js",
    "src/intent.js",
    "src/gate.js",
    "src/modules/tabs.js",
    "src/modules/pwd.js",
    "src/modules/pageExtract.js",
    "src/modules/cookies.js",
    "src/modules/mpris.js",
    "src/modules/downloads.js",
    "src/modules/notifications.js",
    "src/modules/screenlock.js",
  );
} catch (_) {
  // No importScripts in scope (e.g. the vitest harness, which loads
  // every module's globals directly before evaluating this file) —
  // the globals are already defined, so nothing to import.
}

const api = self.qdistroApi;

// Handshake fires on every (re)connect — the bridge rotates its
// session secret on each launch, so a stale extension secret won't
// pass verify_intent_token. Privileged-op sites await
// qdistroIntent.hasSession() before mint().
async function runHandshake() {
  try {
    const reply = await self.qdistroDispatcher.request("qdistro.handshake", {
      proto_version: 1,
    }, { timeoutMs: 5000 });
    if (reply && reply.ok && typeof reply.session_secret_hex === "string") {
      self.qdistroIntent.setSessionSecretHex(reply.session_secret_hex);
    } else {
      console.warn("[qdistro/background] handshake reply missing secret", reply);
    }
  } catch (e) {
    console.warn("[qdistro/background] handshake failed", e && e.message);
  }
}

function bootOnce() {
  if (self.__qdistroBooted) return;
  self.__qdistroBooted = true;

  // Wire context menu (9c).
  if (self.qdistroPageExtract) {
    self.qdistroPageExtract.installContextMenu();
  }
  // Wire downloads listener (9e-2).
  if (self.qdistroDownloads) self.qdistroDownloads.install();
  // Wire notification listeners (9e-3).
  if (self.qdistroNotifications) self.qdistroNotifications.install();

  // Handshake on every (re)connect.
  self.qdistroPort.onConnected(runHandshake);

  // Open the persistent port.
  self.qdistroPort.connect();
}

// onStartup fires when the browser launches; onInstalled fires on
// install/update. Either way we boot exactly once per worker
// lifetime. SW restarts re-execute this file, so __qdistroBooted
// re-initializes — that's the intended shape.
if (api && api.runtime && api.runtime.onStartup) {
  api.runtime.onStartup.addListener(bootOnce);
}
if (api && api.runtime && api.runtime.onInstalled) {
  api.runtime.onInstalled.addListener(bootOnce);
}

// MV3 cold-start: the worker wakes on an event (e.g. a popup
// sendMessage), past onStartup. Boot inline so the port is ready.
bootOnce();

// Per-tab screenlock-inhibit accounting. The compositor's
// idle-inhibit protocol is reference-counted on the bridge side,
// but we still clean up here if a tab vanishes without firing
// pagehide (crashed renderer, kill -9 of the tab process).
const screenlockTabs = new Set();
if (api && api.tabs && api.tabs.onRemoved) {
  api.tabs.onRemoved.addListener((tabId) => {
    if (screenlockTabs.delete(tabId) && self.qdistroScreenlock) {
      self.qdistroScreenlock.release("tab_removed").catch(() => {});
    }
  });
}

// Resolve the popup's extension-internal URL. The build flattens
// src/ to the package root, so popup.html lives at the top level
// (see scripts/build-extension.sh + manifest default_popup).
function popupUrl() {
  try { return api.runtime.getURL("popup.html"); } catch (_) { return null; }
}

// A trusted popup sender: our own extension id, an extension-page
// origin (no sender.tab — content scripts always carry a tab), and
// the sender.url is exactly our popup page. Used to gate consent-
// bearing operations (cookie export) so a content-script bug can't
// mint them.
function isPopupSender(sender) {
  if (!sender || sender.id !== api.runtime.id) return false;
  if (sender.tab) return false; // content scripts carry a tab
  const want = popupUrl();
  return !!want && sender.url === want;
}

// Active-tab URL in the current window, derived in the background
// rather than trusting a caller-supplied req.url.
function activeTabUrl() {
  return new Promise((resolve) => {
    try {
      api.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const t = tabs && tabs.length ? tabs[0] : null;
        resolve(t ? (t.url || "") : "");
      });
    } catch (_) { resolve(""); }
  });
}

// For content-script-initiated pwd ops: the authoritative URL is
// sender.tab.url (set by the browser), never req.url. Reject when
// the content script's claimed URL disagrees with the real frame.
function pwdSenderUrl(req, sender) {
  const tabUrl = (sender && sender.tab && sender.tab.url) || "";
  if (!tabUrl) return { ok: false, error: "no_tab_url" };
  if (req && typeof req.url === "string" && req.url && req.url !== tabUrl) {
    return { ok: false, error: "url_mismatch" };
  }
  // Origin allowlist (options page): when set, only run on listed
  // origins. With all_frames:true the authoritative origin for the
  // allowlist is the SENDING FRAME's URL (sender.url), not the
  // top-level tab URL — a password form in an allowlisted iframe must
  // pass, and a non-allowlisted iframe must be refused regardless of
  // the top frame (codex finding #3). Fall back to tabUrl when the
  // frame URL is unavailable.
  const frameUrl = (sender && sender.url) || tabUrl;
  if (!self.qdistroGate || !self.qdistroGate.isOriginAllowed(frameUrl)) {
    return { ok: false, error: "origin_not_allowed" };
  }
  return { ok: true, url: tabUrl };
}

// Popup ↔ background channel. Popup never owns its own
// connectNative — one host per session.
if (api && api.runtime && api.runtime.onMessage) {
  api.runtime.onMessage.addListener((req, sender, sendResponse) => {
    // Reject anything that isn't our own popup/options page or one
    // of our content scripts. Without `externally_connectable` in
    // the manifest, Chromium already refuses cross-extension and
    // page-context sendMessage calls; this is defense-in-depth so a
    // compromised content script of another extension that somehow
    // reaches us can't drive the bridge.
    if (!sender || sender.id !== api.runtime.id) {
      sendResponse({ ok: false, error: "untrusted_sender" });
      return false;
    }
    (async () => {
      try {
        if (!req || typeof req !== "object") {
          sendResponse({ ok: false, error: "bad_request" });
          return;
        }
        // Options-page module gate: if the user disabled the feature
        // that owns this req.kind, refuse it here — before any intent
        // is minted or the bridge is touched. status/ping carry no
        // module and always pass. (The dispatcher also gates the wire
        // op, but failing here gives the caller a clean, specific
        // error instead of a generic dispatcher rejection.) Await the
        // gate's first storage read for module-mapped kinds so a saved
        // disable wins even on a cold-start message (codex finding #1).
        if (self.qdistroGate && self.qdistroGate.kindModule(req.kind)) {
          if (!self.qdistroGate.isLoaded()) await self.qdistroGate.ready();
          if (!self.qdistroGate.kindEnabled(req.kind)) {
            sendResponse({ ok: false, error: "module_disabled" });
            return;
          }
        }
        switch (req.kind) {
          case "status": {
            sendResponse({
              ok: true,
              connected: self.qdistroPort.isConnected(),
            });
            return;
          }
          case "ping": {
            const r = await self.qdistroDispatcher.request("qdistro.ping", {
              echo: String(Date.now()),
            }, { timeoutMs: 5000 });
            sendResponse({ ok: true, response: r });
            return;
          }
          case "cookies.export": {
            // Consent gate (finding #11): cookie export carries no
            // per-op confirmation, so the ONLY trusted caller is our
            // own popup. Mirror the bundled bridge guard — reject any
            // content-script / non-popup sender, and derive the URL
            // from the active tab instead of trusting req.url.
            if (!isPopupSender(sender)) {
              sendResponse({ ok: false, error: "popup_required" });
              return;
            }
            const url = await activeTabUrl();
            if (!url) {
              sendResponse({ ok: false, error: "no_active_tab" });
              return;
            }
            if (!self.qdistroGate || !self.qdistroGate.isOriginAllowed(url)) {
              sendResponse({ ok: false, error: "origin_not_allowed" });
              return;
            }
            const intent = await self.qdistroIntent.mint("cookies.export");
            const r = await self.qdistroCookies.exportForUrl(url, intent);
            sendResponse({ ok: true, response: r });
            return;
          }

          // ---- content-script entry points ---------------------------
          // Each mints/forwards an intent token where the bridge
          // requires one; tokens carry hmac=null in MVP (see
          // todo/01-intent-tokens.md).

          case "pwd.request_fill": {
            // Finding #10: the page-supplied req.url is untrusted.
            // Derive the URL from sender.tab.url and reject when the
            // content script's claim disagrees with the real frame.
            // The content script also requires a trusted user gesture
            // before sending this; here we only honour requests that
            // carry a genuine tab origin.
            const su = pwdSenderUrl(req, sender);
            if (!su.ok) { sendResponse({ ok: false, error: su.error }); return; }
            const intent = await self.qdistroIntent.mint("pwd.fill");
            const r = await self.qdistroPwd.fill(
              su.url,
              req.username || null,
              intent,
            );
            sendResponse({ ok: true, response: r });
            return;
          }
          case "pwd.request_fill_confirm": {
            // Phase 2 of the two-phase fill. The user has picked one
            // credential from the metadata list returned by
            // pwd.request_fill; redeem the single-use fill_token for
            // the actual password. As with request_fill the URL is
            // derived from sender.tab.url (never the page-supplied
            // req.url), and the username/fill_token must be present —
            // the daemon binds the token to origin+username+peer.
            const su = pwdSenderUrl(req, sender);
            if (!su.ok) { sendResponse({ ok: false, error: su.error }); return; }
            if (typeof req.username !== "string" || !req.username
                || typeof req.fill_token !== "string" || !req.fill_token) {
              sendResponse({ ok: false, error: "invalid_request" });
              return;
            }
            const intent = await self.qdistroIntent.mint("pwd.fill_confirm");
            const r = await self.qdistroPwd.fillConfirm(
              su.url,
              req.username,
              req.fill_token,
              intent,
            );
            sendResponse({ ok: true, response: r });
            return;
          }
          case "pwd.request_save": {
            const su = pwdSenderUrl(req, sender);
            if (!su.ok) { sendResponse({ ok: false, error: su.error }); return; }
            const intent = await self.qdistroIntent.mint("pwd.save");
            const r = await self.qdistroPwd.save(
              su.url,
              req.username || null,
              req.password || "",
              intent,
            );
            sendResponse({ ok: true, response: r });
            return;
          }
          case "mpris.report_update": {
            // Origin allowlist (options page): the authoritative origin
            // is the real frame URL set by the browser (sender.url),
            // not the page-supplied req.url. Fall back to the tab URL.
            const mprisUrl = sender.url || (sender.tab && sender.tab.url) || "";
            if (!self.qdistroGate || !self.qdistroGate.isOriginAllowed(mprisUrl)) {
              sendResponse({ ok: false, error: "origin_not_allowed" });
              return;
            }
            // Fire-and-forget — the page polls 1Hz; we don't want
            // the content script blocked waiting on a wire ack.
            self.qdistroMpris.update({
              title: req.title || "",
              artist: req.artist || "",
              album: req.album || "",
              art_url: req.art_url || "",
              state: req.state || "none",
              position: typeof req.position === "number" ? req.position : null,
              duration: typeof req.duration === "number" ? req.duration : null,
              url: req.url || (sender.url || ""),
              tab_id: (sender.tab && sender.tab.id) || null,
            }).catch(() => {});
            sendResponse({ ok: true });
            return;
          }
          case "screenlock.report_inhibit": {
            const slUrl = sender.url || (sender.tab && sender.tab.url) || "";
            if (!self.qdistroGate || !self.qdistroGate.isOriginAllowed(slUrl)) {
              sendResponse({ ok: false, error: "origin_not_allowed" });
              return;
            }
            const tabId = sender.tab && sender.tab.id;
            if (typeof tabId === "number") screenlockTabs.add(tabId);
            self.qdistroScreenlock.inhibit(req.reason || "fullscreen_video")
              .catch(() => {});
            sendResponse({ ok: true });
            return;
          }
          case "screenlock.report_release": {
            // No origin gate on release: a release must always be able
            // to undo a prior inhibit even if the allowlist changed
            // mid-session (fail-open on the safety-undo direction).
            const tabId = sender.tab && sender.tab.id;
            if (typeof tabId === "number") screenlockTabs.delete(tabId);
            self.qdistroScreenlock.release(req.reason || "fullscreen_exit")
              .catch(() => {});
            sendResponse({ ok: true });
            return;
          }

          default:
            sendResponse({ ok: false, error: "unknown_kind" });
        }
      } catch (e) {
        sendResponse({ ok: false, error: String(e.message || e) });
      }
    })();
    return true; // keep sendResponse channel open for async reply
  });
}
