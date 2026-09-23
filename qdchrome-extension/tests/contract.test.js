// Cross-repo bridge-protocol contract test (Chromium side).
//
// Consumes the shared golden frames (tests/fixtures/golden-frames.js,
// kept byte-identical with the Firefox repo) and drives them against
// the REAL dispatcher handlers and module functions — not stubs:
//
//   - INBOUND frames are fed through dispatcher.handleInbound() and the
//     emitted `<op>.reply` is validated.
//   - OUTBOUND frames are produced by calling the actual module
//     functions and the frame put on the port is validated.
//
// ensures: this extension's handlers accept the canonical request
// frames and produce the canonical reply/outbound frames the bridge —
// and the sibling Firefox extension — agree on. Drift on either side
// (a renamed field, a dropped reply key) fails here.
import { describe, it, expect, beforeEach } from "vitest";
import { createHmac } from "node:crypto";
import { loadExtension, makeFakeChrome, makeFakePort } from "./helpers.js";
import { INBOUND, OUTBOUND } from "./fixtures/golden-frames.js";

describe("bridge protocol contract — INBOUND (bridge → extension)", () => {
  let env;
  let chrome;
  beforeEach(() => {
    chrome = makeFakeChrome();
    // tabs.open handler serializes the created tab; give it a real one.
    chrome.tabs.create = (p, cb) => cb({ id: 99, url: p.url, active: !!p.active, title: "t" });
    chrome.tabs.query = (_q, cb) => cb([{ id: 1, url: "https://a/", title: "A", active: true }]);
    chrome.tabs.remove = (_ids, cb) => cb();
    env = loadExtension({ chrome, portHandle: makeFakePort() });
    env.scope.qdistroPort.connect();
    env.scope.qdistroNotifications.install();
  });

  function replyFor(op) {
    return env.port.sent.find((m) => m.op === op);
  }

  for (const f of INBOUND) {
    it(`accepts ${f.name} and replies with ${f.replyOp}`, async () => {
      await env.scope.qdistroDispatcher.handleInbound(f.request);
      const reply = replyFor(f.replyOp);
      expect(reply, `no ${f.replyOp} emitted`).toBeTruthy();
      expect(reply.request_id).toBe(f.request.request_id);
      if (f.replyMatch) expect(reply).toMatchObject(f.replyMatch);
      for (const k of f.replyKeys || []) {
        expect(reply, `reply missing key ${k}`).toHaveProperty(k);
      }
    });
  }
});

describe("bridge protocol contract — OUTBOUND (extension → bridge)", () => {
  let env;
  let chrome;
  beforeEach(() => {
    chrome = makeFakeChrome();
    chrome.downloads.search = (_q, cb) => cb([{
      id: 11, url: "https://x/a.zip", filename: "/tmp/a.zip",
      state: "in_progress", totalBytes: 4096, bytesReceived: 512,
    }]);
    chrome.cookies.getAll = (_q, cb) => cb([
      { name: "sid", value: "v", domain: ".example.com", path: "/", secure: true },
    ]);
    env = loadExtension({ chrome, portHandle: makeFakePort() });
    env.scope.qdistroPort.connect();
  });

  function sentFor(op) {
    return env.port.sent.find((m) => m.op === op);
  }

  // Returns the intent token `produce` minted (for intentOp frames), or
  // undefined for unprivileged/op_via frames.
  async function produce(f) {
    let minted;
    if (f.op_via === "downloads") {
      env.scope.qdistroDownloads.install();
      chrome.downloads.onChanged.fire({ id: 11, state: { current: "in_progress" } });
    } else {
      // `produce` may mint a real intent token (async); await it and
      // capture the exact token it minted.
      minted = await f.produce(env);
    }
    // The frame is posted synchronously inside the module's
    // dispatcher.request(); poll a few microtask ticks so any
    // callback/Promise chain has landed it before we read.
    for (let i = 0; i < 5; i++) {
      if (sentFor(f.op)) break;
      await new Promise((r) => setTimeout(r, 0));
    }
    return minted;
  }

  for (const f of OUTBOUND) {
    it(`produces a canonical ${f.name} frame`, async () => {
      const minted = await produce(f);
      const frame = sentFor(f.op);
      expect(frame, `no ${f.op} frame produced`).toBeTruthy();
      expect(frame).toMatchObject(f.match);
      for (const k of f.keys || []) {
        expect(frame, `frame missing key ${k}`).toHaveProperty(k);
      }
      // Privileged frames must forward the EXACT token the real intent.js
      // minted — not a placeholder and not a re-fabricated look-alike. The
      // minted token is {request_id, ts, op, hmac} with a genuine sha256
      // HMAC over the session secret; assert its shape, then assert the
      // frame forwarded that very object so a plausible-but-fake token
      // (which the bridge's verify_intent_token would reject) cannot pass.
      if (f.intentOp) {
        expect(minted && typeof minted === "object",
          `${f.op} produce() must mint and return a token`).toBe(true);
        expect(minted.op, "minted token op binds to the operation").toBe(f.intentOp);
        expect(typeof minted.request_id).toBe("string");
        expect(minted.request_id.length).toBeGreaterThan(0);
        expect(typeof minted.ts).toBe("number");
        expect(minted.hmac, "minted token carries a sha256-hex HMAC").toMatch(/^[0-9a-f]{64}$/);
        expect(frame.intent_token,
          "frame forwards the exact minted token").toEqual(minted);
      }
    });
  }
});

// The OUTBOUND contract above proves the extension FORWARDS the exact token
// intent.js minted, but it cannot see whether that token's HMAC is one the
// native bridge would actually ACCEPT — a mint() that signed the wrong
// canonical (field order, separator, encoding) would still pass there. This
// known-answer test closes that gap: it pins intent.js's HMAC to the bridge's
// canonical `request_id|ts|op` (qdistro/browser_bridge/qdistro_browser_bridge.py
// _compute_token_hmac: HMAC-SHA256 over UTF-8 `request_id|ts|op` keyed by the
// raw session-secret bytes, hex). The pinned vector below was computed from BOTH
// node:crypto and the Python bridge and matches byte-for-byte.
describe("intent token HMAC matches the bridge canonical (known-answer)", () => {
  // Same secret helpers.js seeds by default; pinned so the KAT is self-contained.
  const SECRET_HEX =
    "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef";
  // Reference oracle: an INDEPENDENT HMAC impl (node:crypto, vs intent.js's
  // crypto.subtle) over the bridge's exact canonical string.
  const bridgeHmac = (request_id, ts, op) =>
    createHmac("sha256", Buffer.from(SECRET_HEX, "hex"))
      .update(`${request_id}|${ts}|${op}`, "utf8").digest("hex");

  it("reference oracle equals the precomputed bridge digest (integral ts)", () => {
    // Locks the oracle itself so it can't silently drift; this exact value is
    // also produced by the Python bridge's _compute_token_hmac.
    expect(bridgeHmac("rid-1", 1700000000, "pwd.fill")).toBe(
      "342b14a532db956dc362f31d58daa6507208a540423e9c19e76734908a355938");
  });

  it("oracle matches the Python bridge for a FRACTIONAL ts (float stringification)", () => {
    // The integral vector above can't catch the one cross-stack hazard that
    // actually bites the runtime mint() path: `ts = Date.now()/1000` is a
    // FLOAT, and JS Number->string must agree with Python float->string inside
    // the canonical `request_id|ts|op`. The digest below was produced by the
    // REAL bridge `_compute_token_hmac("rid-1", 1700000000.123, "pwd.fill")`;
    // it matches node:crypto here only because both ends shortest-round-trip
    // the fractional value to the identical "1700000000.123". (Whole-second ts
    // is NOT tested as a Python float on purpose: that state is unreachable —
    // JSON.stringify serializes a whole `ts` as an integer, which Python parses
    // back as `int`, so the integral vector above already pins that path. A
    // Python float 1700000000.0 would stringify to "1700000000.0" and diverge,
    // but the wire never carries it.)
    expect(bridgeHmac("rid-1", 1700000000.123, "pwd.fill")).toBe(
      "f3b814bb881010254137237297d328b2560b00fee52f00e4f38af562ed1df786");
  });

  it("mint() signs request_id|ts|op exactly as the bridge verifies", async () => {
    const env = loadExtension({ chrome: makeFakeChrome(), portHandle: makeFakePort() });
    env.scope.qdistroIntent.setSessionSecretHex(SECRET_HEX);
    const tok = await env.scope.qdistroIntent.mint("pwd.fill");
    expect(tok.hmac, "intent.js HMAC must match the bridge canonical")
      .toBe(bridgeHmac(tok.request_id, tok.ts, tok.op));
    // Negative control: a permuted canonical (op|ts|request_id) must NOT match,
    // proving the assertion discriminates field order — the exact drift that
    // would otherwise ship green.
    expect(tok.hmac).not.toBe(bridgeHmac(tok.op, tok.ts, tok.request_id));
  });
});
