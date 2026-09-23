# Real intent tokens

`src/intent.js` is explicit MVP shape. `mint()` returns
`{operation, timestamp, ttl_ms, nonce, hmac}` where `hmac` is always
`null` until the bridge runs the handshake, and the placeholder
function returns `null` — see `hmacPlaceholder()` at intent.js:33.

The bridge will reject any token with `hmac=null` once Phase-9d
lands, which is the intended fail-closed posture: the extension code
path is in place, the security gate sits on the daemon side, and
shipping without the daemon counterpart breaks safely.

## Deliverables

### 1. `qdistro.handshake` round-trip

On port (re)connect, the extension sends:

```
{ op: "qdistro.handshake",
  ext_pub: <base64 X25519 pub key from extension>,
  request_id: <fresh> }
```

The bridge replies:

```
{ op: "qdistro.handshake.reply",
  request_id: ...,
  bridge_pub: <base64 X25519 pub from bridge>,
  ok: true }
```

Both sides derive the session secret via X25519 ECDH + HKDF-SHA256
with `info = "qdistro-intent-token-v1"`. Re-run on every reconnect;
the secret never crosses the wire.

Implementation lives in `src/intent.js` (`installHandshake(dispatcher)`
exported off `qdistroIntent`). The dispatcher in `src/dispatcher.js`
must learn the `qdistro.handshake.reply` op and hand the payload to
`qdistroIntent.completeHandshake`.

### 2. Real HMAC

Replace `hmacPlaceholder()` with:

```js
async function sign(token) {
  const enc = new TextEncoder().encode(canonicalize(token));
  const key = await crypto.subtle.importKey(
    "raw", sessionSecret, { name: "HMAC", hash: "SHA-256" },
    false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc);
  return base64(sig);
}
```

`canonicalize()` must produce a stable byte sequence (sorted keys,
fixed separator) so bridge-side verify lines up bit-for-bit. Pick
JCS (RFC 8785) over hand-rolled canonical JSON — the spec is short
and shippable in ~40 LOC.

### 3. Nonce store + single-use semantics

Bridge-side responsibility, but the extension carries the matching
contract: every `mint()` increments a monotonic `counter` and embeds
it in `nonce`. The bridge keeps a per-session set of seen nonces
and rejects replays. TTL window: 5 s by default; tunable via
`mint(op, ttlMs)`.

### 4. Scope binding

A token minted for `cookies.export` must not validate against
`pwd.save`. Already enforced extension-side by including `operation`
in the canonicalization input, but the test in `tests/intent.test.js`
("scope: token for cookies.export doesn't validate against pwd.save")
currently asserts the MVP placeholder behaviour. Update once the HMAC
lands.

## Tests to update

- `tests/intent.test.js` — replace placeholder-behaviour assertions
  with real HMAC round-trip cases. Drive `crypto.subtle` via the
  `@peculiar/webcrypto` polyfill or Node's built-in `globalThis.crypto`
  (available in 19+; the project already targets 20+).
- New `tests/handshake.test.js` — `installHandshake` + dispatcher
  integration, retry on reconnect, downgrade refusal if `bridge_pub`
  is missing.

## Acceptance criteria

1. Reconnect → handshake runs → `sessionSecret` set.
2. `mint("cookies.export")` produces a non-null `hmac` after handshake.
3. Bridge-side verify accepts a freshly-minted token, rejects an
   expired one (clock-skewed test), rejects a replay (same nonce
   twice), rejects an op-mismatched token.
4. Handshake failure leaves `sessionSecret = null` and `mint()` falls
   back to MVP behaviour so the bridge sees the rejection it expects.

## Cross-references

- Bridge side: `../../qdistro/doc/browser.md` §Intent tokens.
- Threat scope is unchanged from `qdistro/doc/browser.md` §Intent
  tokens: defends against replay + page-script-triggered calls,
  does not defend against a compromised extension or browser.
