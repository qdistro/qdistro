// Port lifecycle tests: connect, heartbeat-ack, disconnect-and-reconnect,
// heartbeat-watchdog teardown, escalating backoff, connectNative throw.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { loadExtension, makeFakePort, makeFakeChrome } from "./helpers.js";

describe("qdistroPort", () => {
  let env;

  beforeEach(() => {
    env = loadExtension();
    env.scope.qdistroPort.connect();
  });

  it("opens a port on connect()", () => {
    expect(env.scope.qdistroPort.isConnected()).toBe(true);
  });

  it("replies to qdistro.heartbeat with qdistro.heartbeat.ack", () => {
    env.port.deliver({ op: "qdistro.heartbeat", echo: "tick-1" });
    expect(env.port.sent.at(-1)).toEqual({
      op: "qdistro.heartbeat.ack",
      echo: "tick-1",
    });
  });

  it("transitions to disconnected on port.disconnect", () => {
    env.port.triggerDisconnect();
    expect(env.scope.qdistroPort.isConnected()).toBe(false);
  });

  it("schedules a reconnect after disconnect", async () => {
    vi.useFakeTimers();
    const env2 = loadExtension();
    env2.scope.qdistroPort.connect();
    env2.port.triggerDisconnect();
    expect(env2.scope.qdistroPort.isConnected()).toBe(false);
    // Backoff starts at 1000ms — advance time and the port reconnects.
    vi.advanceTimersByTime(1100);
    expect(env2.scope.qdistroPort.isConnected()).toBe(true);
    vi.useRealTimers();
  });

  // ensures: a silent bridge (heartbeats stop arriving) is detected by
  // the 60s watchdog, which tears the port down and reconnects — the
  // extension never sits forever on a dead-but-not-disconnected port.
  it("heartbeat watchdog tears down and reconnects when heartbeats stop", () => {
    vi.useFakeTimers();
    try {
      const env2 = loadExtension();
      env2.scope.qdistroPort.connect();
      const firstPort = env2.port.port;
      let disconnected = false;
      // Spy on disconnect() to prove forceReconnect() severs the old port.
      const origDisconnect = firstPort.disconnect;
      firstPort.disconnect = (...a) => { disconnected = true; return origDisconnect.apply(firstPort, a); };
      expect(env2.scope.qdistroPort.isConnected()).toBe(true);

      // No inbound message for the full 60s timeout → watchdog trips.
      // HEARTBEAT_TIMEOUT_MS is 60000; advance just past it.
      vi.advanceTimersByTime(60000);
      expect(disconnected).toBe(true);
      expect(env2.scope.qdistroPort.isConnected()).toBe(false);

      // forceReconnect scheduled a reconnect at the 1s backoff floor.
      vi.advanceTimersByTime(1000);
      expect(env2.scope.qdistroPort.isConnected()).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  // ensures: an inbound message rearms the watchdog, so a healthy
  // (chatty) bridge is never spuriously reconnected.
  it("an inbound message rearms the heartbeat watchdog", () => {
    vi.useFakeTimers();
    try {
      const env2 = loadExtension();
      env2.scope.qdistroPort.connect();
      // Just before the deadline, deliver a heartbeat — resets the timer.
      vi.advanceTimersByTime(59000);
      env2.port.deliver({ op: "qdistro.heartbeat", echo: "still-here" });
      // Original deadline would have tripped here; instead it's been
      // pushed out a fresh 60s.
      vi.advanceTimersByTime(2000);
      expect(env2.scope.qdistroPort.isConnected()).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  // ensures: repeated failed reconnects back off exponentially (1s, 2s,
  // 4s, ...) capped at 60s, rather than hammering connectNative — so a
  // down bridge doesn't spin the worker.
  it("escalates reconnect backoff (1s → 2s → 4s) while connectNative keeps failing", () => {
    vi.useFakeTimers();
    try {
      const env2 = loadExtension();
      env2.scope.qdistroPort.connect();
      const state = env2.scope.qdistroPort._state;
      // First connect succeeded; backoff floor is 1000.
      expect(state.backoffMs).toBe(1000);

      // Make every subsequent connectNative throw so reconnects fail
      // and the backoff keeps climbing.
      env2.scope.chrome.runtime.connectNative = () => { throw new Error("host_down"); };

      // Disconnect → scheduleReconnect uses 1000, then doubles to 2000.
      env2.port.triggerDisconnect();
      expect(env2.scope.qdistroPort.isConnected()).toBe(false);
      expect(state.backoffMs).toBe(2000);

      // Fire the 1s timer → connect() throws → schedules with 2000, doubles to 4000.
      vi.advanceTimersByTime(1000);
      expect(env2.scope.qdistroPort.isConnected()).toBe(false);
      expect(state.backoffMs).toBe(4000);

      // Fire the 2s timer → connect() throws again → schedules with 4000, doubles to 8000.
      vi.advanceTimersByTime(2000);
      expect(state.backoffMs).toBe(8000);
    } finally {
      vi.useRealTimers();
    }
  });

  // ensures: backoff is CAPPED — a long outage doesn't grow the retry
  // delay unbounded past 60s.
  it("caps reconnect backoff at 60s", () => {
    vi.useFakeTimers();
    try {
      const env2 = loadExtension();
      env2.scope.qdistroPort.connect();
      const state = env2.scope.qdistroPort._state;
      env2.scope.chrome.runtime.connectNative = () => { throw new Error("host_down"); };
      env2.port.triggerDisconnect();
      // Drive many failed reconnects; advance generously past the cap.
      for (let i = 0; i < 12; i++) {
        vi.advanceTimersByTime(60000);
      }
      expect(state.backoffMs).toBe(60000);
    } finally {
      vi.useRealTimers();
    }
  });

  // ensures: a connectNative that throws on the INITIAL connect is
  // caught (not propagated), leaves the port disconnected, and still
  // schedules a recovery attempt.
  it("connectNative throwing on initial connect is caught and schedules a retry", () => {
    vi.useFakeTimers();
    try {
      const env2 = loadExtension();
      env2.scope.chrome.runtime.connectNative = () => { throw new Error("connect_threw"); };
      const seen = [];
      env2.scope.qdistroPort.onStatus((s) => seen.push(s));
      expect(() => env2.scope.qdistroPort.connect()).not.toThrow();
      expect(env2.scope.qdistroPort.isConnected()).toBe(false);
      // Status listener saw the failure with an error string.
      const failure = seen.find((s) => s.connected === false);
      expect(failure).toBeTruthy();
      expect(failure.error).toMatch(/connect_threw/);

      // A retry was scheduled at the 1s floor; let it succeed this time.
      env2.scope.chrome.runtime.connectNative = () => env2.port.port;
      vi.advanceTimersByTime(1000);
      expect(env2.scope.qdistroPort.isConnected()).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });
});
