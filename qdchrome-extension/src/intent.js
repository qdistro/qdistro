// Intent token minting — aligned with the bridge's
// `verify_intent_token` shape (see qdistro/browser_bridge/
// qdistro_browser_bridge.py:512).
//
// Token shape (the bridge's expectation, NOT the earlier MVP shape):
//
//   {
//     request_id: "<unique string>",
//     ts: <unix seconds (float)>,
//     op: "<operation name>",
//     hmac: "<sha256 hex of `request_id|ts|op` keyed with session secret>"
//   }
//
// The session secret comes from `qdistro.handshake`. Until the
// handshake completes, mint() throws — every privileged op would
// fail bridge-side anyway with `missing_intent_token`, so failing
// fast here is the cheaper signal.
//
// Bridge TTL is 5s (constant in the bridge); we don't include ttl
// in the token because the bridge derives it from `ts`.
//
// @ts-check
(function (root) {
  "use strict";

  let sessionSecretBytes = null;     // Uint8Array, raw secret from handshake
  let cryptoKeyPromise = null;       // Promise<CryptoKey> for HMAC

  function hex(buf) {
    const bytes = new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < bytes.length; i++) {
      const h = bytes[i].toString(16);
      s += h.length === 1 ? "0" + h : h;
    }
    return s;
  }

  function hexToBytes(s) {
    const len = s.length / 2;
    const out = new Uint8Array(len);
    for (let i = 0; i < len; i++) {
      out[i] = parseInt(s.substr(i * 2, 2), 16);
    }
    return out;
  }

  function randomId() {
    // 16 bytes hex — collision space wide enough for the 5s TTL
    // replay window (bridge enforces single-use within window).
    const buf = new Uint8Array(16);
    (root.crypto || globalThis.crypto).getRandomValues(buf);
    return hex(buf);
  }

  /**
   * Install the session secret from a handshake reply.
   * @param {string} secretHex hex-encoded raw secret bytes
   */
  function setSessionSecretHex(secretHex) {
    if (!secretHex || typeof secretHex !== "string") {
      sessionSecretBytes = null;
      cryptoKeyPromise = null;
      return;
    }
    sessionSecretBytes = hexToBytes(secretHex);
    const subtle = (root.crypto || globalThis.crypto).subtle;
    cryptoKeyPromise = subtle.importKey(
      "raw",
      sessionSecretBytes,
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
  }

  function hasSession() {
    return sessionSecretBytes !== null && cryptoKeyPromise !== null;
  }

  /**
   * Mint a fresh intent token for `op`. Async because HMAC via
   * crypto.subtle is async. Throws if no session secret is set —
   * the caller (a privileged-op site) must wait for the handshake
   * before minting.
   */
  async function mint(op) {
    if (!hasSession()) {
      throw new Error("intent_no_session");
    }
    const request_id = randomId();
    const ts = Date.now() / 1000;
    const opStr = String(op || "");
    const canonical = `${request_id}|${ts}|${opStr}`;
    const subtle = (root.crypto || globalThis.crypto).subtle;
    const key = await cryptoKeyPromise;
    const sigBuf = await subtle.sign("HMAC", key,
      new TextEncoder().encode(canonical));
    return {
      request_id,
      ts,
      op: opStr,
      hmac: hex(sigBuf),
    };
  }

  // Compat: older tests / callers used `setSessionSecret(secret)` and
  // synchronous `mint()`. Keep names compatible but redirect to the
  // new shape — tests that don't call setSessionSecretHex first will
  // see mint() throw.
  function setSessionSecret(secret) {
    if (secret === null || secret === undefined) {
      sessionSecretBytes = null;
      cryptoKeyPromise = null;
      return;
    }
    // Treat string as hex for backward compatibility with tests
    // that called setSessionSecret("xxx"). Anything non-string is
    // a misuse — leave the session unset.
    if (typeof secret === "string") setSessionSecretHex(secret);
  }

  function ttlMs() {
    // Informational only — the bridge owns the actual TTL constant.
    // 5s matches `INTENT_TOKEN_TTL_S` in the bridge as of 2026-05.
    return 5000;
  }

  root.qdistroIntent = {
    mint,
    setSessionSecretHex,
    setSessionSecret,
    hasSession,
    ttlMs,
  };
})(typeof self !== "undefined" ? self : globalThis);
