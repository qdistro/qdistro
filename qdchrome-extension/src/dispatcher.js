// request_id-correlated dispatcher.
//
// Two flows:
//
//   1. Bridge-initiated request (daemon → bridge → extension):
//      bridge sends `{op: "tabs.list", request_id: "rN-hex"}` over
//      the port. request_id is a string (e.g. "r1-abc123"); the
//      dispatcher accepts any truthy value (`!= null`).
//      Dispatcher routes by op to a registered handler in
//      src/modules/, awaits the handler's reply payload, then sends
//      `{op: "tabs.list.reply", request_id: "rN-hex", ...payload}` back.
//      Per spec/14 Phase-9b §"Timeout behavior when MV3 service worker
//      suspends" — the bridge handles retry; we just respond.
//
//   2. Extension-initiated request (popup click → extension → bridge → daemon):
//      caller invokes `qdistroDispatcher.request("cookies.export", body)`
//      which assigns a request_id and returns a Promise. The dispatcher
//      keeps a Map<request_id, {resolve,reject,timer}> until a matching
//      `.reply` arrives.
//
// Borrowed from KDE Plasma's `SettingsManager.executeMethod` shape —
// outbound requests use integer request_ids (from nextRequestId++);
// inbound bridge-initiated requests use string request_ids (e.g.
// "r1-abc123"). Both types are accepted — the dispatcher checks
// `!= null`, not typeof. Timeouts per-request, dispose on disconnect.
//
// @ts-check
(function (root) {
  "use strict";
  const port = root.qdistroPort;
  const DEFAULT_TIMEOUT_MS = 10000;

  const handlers = new Map();       // op → async (body, identity) => replyBody
  const pending = new Map();        // request_id → {resolve, reject, timer, op}
  let nextRequestId = 1;

  function log(...args) {
    try { console.log("[qdistro/dispatch]", ...args); } catch (_) { /* SW */ }
  }

  // Module gate (options-page toggles). Looked up lazily because
  // gate.js loads AFTER dispatcher.js in the boot order; with no gate
  // present at all (it never loaded) an op passes — that fail-open is
  // only for a genuinely-absent enforcement layer, never for a disabled
  // flag. Infrastructure ops (qdistro.*) carry no module and pass.
  function opAllowed(op) {
    const gate = root.qdistroGate;
    if (!gate) return true;
    return gate.opEnabled(op);
  }

  // Await the gate's first storage read so a disabled-module flag is
  // honoured even on the worker's cold-start event (codex finding #1).
  // Returns null when there's nothing to wait for (infrastructure op,
  // no gate, or the gate has already loaded) — callers skip the await
  // so the steady-state path stays synchronous.
  function gateReadyIfPending(op) {
    const gate = root.qdistroGate;
    if (!gate || !gate.opModule(op) || !gate.ready) return null;
    if (gate.isLoaded && gate.isLoaded()) return null;
    return gate.ready();
  }

  function register(op, handler) {
    if (handlers.has(op)) {
      log(`overwriting handler for ${op}`);
    }
    handlers.set(op, handler);
  }

  async function handleInbound(msg) {
    if (!msg || typeof msg !== "object") return;
    const op = String(msg.op || "");
    if (!op) return;

    // Reply to an outbound request we initiated.
    if (op.endsWith(".reply") && msg.request_id != null) {
      const slot = pending.get(msg.request_id);
      if (!slot) {
        log("orphan reply", op, msg.request_id);
        return;
      }
      if (op !== `${slot.op}.reply`) {
        log("mismatched reply", op, "expected", `${slot.op}.reply`, msg.request_id);
        return;
      }
      pending.delete(msg.request_id);
      if (slot.timer) clearTimeout(slot.timer);
      slot.resolve(msg);
      return;
    }

    // Inbound op from the bridge. Gate disabled modules BEFORE the
    // handler runs — a disabled feature must not act on a bridge
    // request. On cold start (gate config not yet read) await the first
    // read so the event can't slip past a saved disable; in steady
    // state the check is synchronous. Reply with a deterministic error
    // rather than dropping it so the bridge isn't left waiting.
    const pending2 = gateReadyIfPending(op);
    if (pending2) await pending2;
    if (!opAllowed(op)) {
      log("inbound op gated (module disabled)", op);
      if (msg.request_id != null) {
        port.send({
          op: `${op}.reply`,
          request_id: msg.request_id,
          ok: false,
          error: "module_disabled",
        });
      }
      return;
    }
    const h = handlers.get(op);
    if (!h) {
      log("no handler for inbound op", op);
      if (msg.request_id != null) {
        port.send({
          op: `${op}.reply`,
          request_id: msg.request_id,
          ok: false,
          error: "unknown_op",
        });
      }
      return;
    }
    try {
      const body = (await h(msg)) || {};
      if (msg.request_id != null) {
        port.send({
          op: `${op}.reply`,
          request_id: msg.request_id,
          ok: true,
          ...body,
        });
      }
    } catch (e) {
      log("handler threw", op, e);
      if (msg.request_id != null) {
        port.send({
          op: `${op}.reply`,
          request_id: msg.request_id,
          ok: false,
          error: "handler_raised",
          detail: String(e).slice(0, 200),
        });
      }
    }
  }

  function _send(op, body, opts) {
    const request_id = nextRequestId++;
    const timeoutMs = opts.timeoutMs || DEFAULT_TIMEOUT_MS;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (pending.has(request_id)) {
          pending.delete(request_id);
          reject(new Error(`timeout: ${op}`));
        }
      }, timeoutMs);
      pending.set(request_id, { resolve, reject, timer, op });
      const sent = port.send({ op, request_id, ...(body || {}) });
      if (!sent) {
        clearTimeout(timer);
        pending.delete(request_id);
        reject(new Error("port_disconnected"));
      }
    });
  }

  function request(op, body, opts) {
    opts = opts || {};
    // Gate extension-initiated ops too: a disabled module must not push
    // to the bridge (e.g. a content script reporting MPRIS while MPRIS
    // is off). Reject before touching the port.
    //
    // Fast path: once the gate's first storage read has landed (the
    // common steady state), enforce synchronously so the wire send
    // happens on the same tick — callers (and tests) that inspect the
    // outbound frame synchronously rely on that.
    //
    // Slow path: only for a module-mapped op whose gate config hasn't
    // loaded yet (cold start) do we await ready() so a saved disable
    // wins (codex finding #1). Infrastructure ops (qdistro.*) never
    // wait — no module.
    const gate = root.qdistroGate;
    if (gate && gate.opModule(op) && gate.ready && !gate.isLoaded()) {
      return gate.ready().then(() => {
        if (!opAllowed(op)) throw new Error("module_disabled");
        return _send(op, body, opts);
      });
    }
    if (!opAllowed(op)) {
      return Promise.reject(new Error("module_disabled"));
    }
    return _send(op, body, opts);
  }

  function _resetForTests() {
    for (const slot of pending.values()) {
      if (slot.timer) clearTimeout(slot.timer);
    }
    pending.clear();
    handlers.clear();
    nextRequestId = 1;
  }

  port.onMessage(handleInbound);

  root.qdistroDispatcher = {
    register,
    request,
    handlers,    // tests inspect
    pending,     // tests inspect
    handleInbound, // tests drive directly
    _resetForTests,
  };
})(typeof self !== "undefined" ? self : globalThis);
