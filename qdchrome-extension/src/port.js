// Persistent native-messaging port manager.
//
// Owns the single qdistro connectNative port for the lifetime of
// the background service worker. Reconnects on disconnect with
// exponential backoff (1s → 60s cap). Pumps inbound messages to
// the dispatcher. Tracks 25s heartbeat — bridge sends
// qdistro.heartbeat, we reply qdistro.heartbeat.ack, both sides
// reset their inactivity timers.
//
// Borrowed shape from KDE Plasma browser integration's
// SettingsConnection: a single source of truth for "is the port up,"
// per-feature modules subscribe via dispatcher.register rather than
// holding their own ports. One native host = one port — MV3 only
// allows so many concurrent native-messaging children.
//
// Exposed as `self.qdistroPort` (module-global on the worker scope).
//
// @ts-check
(function (root) {
  "use strict";
  const api = root.qdistroApi;
  const HOST = "qdistro";
  const HEARTBEAT_TIMEOUT_MS = 60 * 1000; // 25s + 35s grace; bridge drives the 25s cadence
  const BACKOFF_INITIAL_MS = 1000;
  const BACKOFF_CAP_MS = 60 * 1000;

  const state = {
    port: null,
    connected: false,
    backoffMs: BACKOFF_INITIAL_MS,
    reconnectTimer: null,
    heartbeatTimer: null,
    lastHeartbeatAt: 0,
    listeners: new Set(),
    statusListeners: new Set(),
    connectedListeners: new Set(),
  };

  function log(...args) {
    try { console.log("[qdistro/port]", ...args); } catch (_) { /* SW lifecycle */ }
  }

  function setStatus(status) {
    for (const cb of state.statusListeners) {
      try { cb(status); } catch (e) { log("status listener threw", e); }
    }
  }

  function armHeartbeatWatchdog() {
    if (state.heartbeatTimer) clearTimeout(state.heartbeatTimer);
    state.heartbeatTimer = setTimeout(() => {
      log("heartbeat watchdog tripped — forcing reconnect");
      forceReconnect("heartbeat_timeout");
    }, HEARTBEAT_TIMEOUT_MS);
  }

  function onMessage(msg) {
    state.lastHeartbeatAt = Date.now();
    armHeartbeatWatchdog();
    // Heartbeat is handled inline so it survives even if the
    // dispatcher hasn't loaded yet (worker cold start race).
    if (msg && msg.op === "qdistro.heartbeat") {
      send({ op: "qdistro.heartbeat.ack", request_id: msg.request_id, echo: msg.echo || null });
      return;
    }
    for (const cb of state.listeners) {
      try { cb(msg); } catch (e) { log("listener threw", e, msg); }
    }
  }

  function onDisconnect() {
    const err = api.runtime.lastError;
    log("port disconnected", err && err.message);
    state.connected = false;
    state.port = null;
    if (state.heartbeatTimer) clearTimeout(state.heartbeatTimer);
    setStatus({ connected: false, error: err && err.message });
    scheduleReconnect();
  }

  function scheduleReconnect() {
    if (state.reconnectTimer) return;
    const delay = state.backoffMs;
    log(`reconnect in ${delay}ms`);
    state.reconnectTimer = setTimeout(() => {
      state.reconnectTimer = null;
      connect();
    }, delay);
    state.backoffMs = Math.min(state.backoffMs * 2, BACKOFF_CAP_MS);
  }

  function connect() {
    if (state.connected) return;
    try {
      log("connectNative", HOST);
      const port = api.runtime.connectNative(HOST);
      state.port = port;
      state.connected = true;
      state.backoffMs = BACKOFF_INITIAL_MS;
      port.onMessage.addListener(onMessage);
      port.onDisconnect.addListener(onDisconnect);
      armHeartbeatWatchdog();
      setStatus({ connected: true });
      // Fire onConnected hooks (handshake, etc.) after the port is
      // wired so a hook can immediately dispatcher.request against
      // the fresh secret.
      for (const cb of state.connectedListeners) {
        try { cb(); } catch (e) { log("onConnected hook threw", e); }
      }
    } catch (e) {
      log("connectNative threw", e);
      state.connected = false;
      state.port = null;
      setStatus({ connected: false, error: String(e) });
      scheduleReconnect();
    }
  }

  function forceReconnect(reason) {
    log("forceReconnect", reason);
    try { if (state.port) state.port.disconnect(); } catch (_) { /* ignore */ }
    state.connected = false;
    state.port = null;
    scheduleReconnect();
  }

  function send(payload) {
    if (!state.connected || !state.port) {
      log("send while disconnected; dropping", payload && payload.op);
      return false;
    }
    try {
      state.port.postMessage(payload);
      return true;
    } catch (e) {
      log("postMessage threw", e);
      forceReconnect("post_threw");
      return false;
    }
  }

  function onMessageRegister(cb) { state.listeners.add(cb); }
  function onStatus(cb) { state.statusListeners.add(cb); }
  function onConnected(cb) { state.connectedListeners.add(cb); }

  root.qdistroPort = {
    connect,
    send,
    onMessage: onMessageRegister,
    onStatus,
    onConnected,
    isConnected: () => state.connected,
    // Test seam: tests replace the runtime to inject a fake port.
    _resetForTests: () => {
      if (state.reconnectTimer) clearTimeout(state.reconnectTimer);
      if (state.heartbeatTimer) clearTimeout(state.heartbeatTimer);
      state.port = null;
      state.connected = false;
      state.backoffMs = BACKOFF_INITIAL_MS;
      state.reconnectTimer = null;
      state.heartbeatTimer = null;
      state.listeners.clear();
      state.statusListeners.clear();
      state.connectedListeners.clear();
    },
    _state: state,
  };
})(typeof self !== "undefined" ? self : globalThis);
