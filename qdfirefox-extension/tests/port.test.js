// Port lifecycle: connect, heartbeat-ack, disconnect-and-reconnect,
// heartbeat-watchdog teardown, escalating backoff, connectNative throw.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { loadExtension } from "./helpers.js";

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

  it("schedules a reconnect after disconnect", () => {
    vi.useFakeTimers();
    try {
      const env2 = loadExtension();
      env2.scope.qdistroPort.connect();
      env2.port.triggerDisconnect();
      expect(env2.scope.qdistroPort.isConnected()).toBe(false);
      vi.advanceTimersByTime(1100);
      expect(env2.scope.qdistroPort.isConnected()).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("send() returns false when disconnected", () => {
    env.port.triggerDisconnect();
    const ok = env.scope.qdistroPort.send({ op: "test" });
    expect(ok).toBe(false);
  });

  it("statusListeners are notified on connect/disconnect", () => {
    const env2 = loadExtension();
    const seen = [];
    env2.scope.qdistroPort.onStatus((s) => seen.push(s));
    env2.scope.qdistroPort.connect();
    env2.port.triggerDisconnect();
    expect(seen.some((s) => s.connected === true)).toBe(true);
    expect(seen.some((s) => s.connected === false)).toBe(true);
  });

  // ensures: a silent bridge (heartbeats stop arriving) is detected by
  // the 60s watchdog, which tears the port down and reconnects — the
  // event page never sits forever on a dead-but-not-disconnected port.
  it("heartbeat watchdog tears down and reconnects when heartbeats stop", () => {
    vi.useFakeTimers();
    try {
      const env2 = loadExtension();
      env2.scope.qdistroPort.connect();
      const firstPort = env2.port.port;
      let disconnected = false;
      const origDisconnect = firstPort.disconnect;
      firstPort.disconnect = (...a) => { disconnected = true; return origDisconnect.apply(firstPort, a); };
      expect(env2.scope.qdistroPort.isConnected()).toBe(true);

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
      vi.advanceTimersByTime(59000);
      env2.port.deliver({ op: "qdistro.heartbeat", echo: "still-here" });
      // Original deadline would have tripped here; instead it's pushed
      // out a fresh 60s.
      vi.advanceTimersByTime(2000);
      expect(env2.scope.qdistroPort.isConnected()).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  // ensures: repeated failed reconnects back off exponentially (1s, 2s,
  // 4s, ...) rather than hammering connectNative — so a down bridge
  // doesn't spin the event page.
  it("escalates reconnect backoff (1s → 2s → 4s) while connectNative keeps failing", () => {
    vi.useFakeTimers();
    try {
      const env2 = loadExtension();
      env2.scope.qdistroPort.connect();
      const state = env2.scope.qdistroPort._state;
      expect(state.backoffMs).toBe(1000);

      env2.scope.browser.runtime.connectNative = () => { throw new Error("host_down"); };

      env2.port.triggerDisconnect();
      expect(env2.scope.qdistroPort.isConnected()).toBe(false);
      expect(state.backoffMs).toBe(2000);

      vi.advanceTimersByTime(1000);
      expect(env2.scope.qdistroPort.isConnected()).toBe(false);
      expect(state.backoffMs).toBe(4000);

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
      env2.scope.browser.runtime.connectNative = () => { throw new Error("host_down"); };
      env2.port.triggerDisconnect();
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
      env2.scope.browser.runtime.connectNative = () => { throw new Error("connect_threw"); };
      const seen = [];
      env2.scope.qdistroPort.onStatus((s) => seen.push(s));
      expect(() => env2.scope.qdistroPort.connect()).not.toThrow();
      expect(env2.scope.qdistroPort.isConnected()).toBe(false);
      const failure = seen.find((s) => s.connected === false);
      expect(failure).toBeTruthy();
      expect(failure.error).toMatch(/connect_threw/);

      env2.scope.browser.runtime.connectNative = () => env2.port.port;
      vi.advanceTimersByTime(1000);
      expect(env2.scope.qdistroPort.isConnected()).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });
});
