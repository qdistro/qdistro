# 06 — Real intent tokens (HMAC via crypto.subtle) — DONE 2026-05-16

> Initial implementation landed alongside [08-bridge-protocol-alignment.md](08-bridge-protocol-alignment.md). Token shape, HMAC mint, and handshake-on-connect are in place. Open items: replay-resistance accounting (bridge enforces single-use; extension should also drop a fresh request_id per send to avoid client-side replays — currently relies on `crypto.getRandomValues` for uniqueness). Cross-session secret rotation handled by re-handshake on every reconnect.


## Why

`src/intent.js` mints tokens with `hmac=null`. The bridge daemon's verification is the only thing currently keeping a malicious page from triggering `cookies.export` or `pwd.fill` via injected `runtime.sendMessage` — and a malicious page can't directly call `sendMessage` without `externally_connectable`, but a compromised content script of *this* extension could. The MVP shape is acceptable for local-dev testing; it is **not** acceptable for any ship to a user.

## Design

Per qdistro spec/14 Phase-9d:

1. On extension boot, send `qdistro.handshake` with an X25519 public key generated via `crypto.subtle.generateKey({name:"ECDH", namedCurve:"P-256"})` (Firefox doesn't expose X25519 directly; P-256 is the closest portable curve).
2. Bridge replies with its own public key. Both sides derive a shared secret via ECDH and use HKDF to produce a 32-byte HMAC-SHA-256 key. Session secret lives in memory only; never persisted.
3. `intent.mint()` HMACs `{operation, timestamp, ttl_ms, nonce}` with the session key. Bridge verifies the HMAC, the TTL (`now - timestamp < ttl_ms`), and that the nonce hasn't been seen within the TTL window.
4. On bridge restart or extension reload, the handshake re-runs. Tokens minted under the old key are rejected; the extension catches the rejection and re-handshakes once before bubbling the error up.

## Concrete changes

- `src/intent.js`: replace the `hmacPlaceholder()` stub with real `crypto.subtle.sign("HMAC", sessionKey, payload)`. Async; `mint()` must become `async`. Callers update.
- `src/dispatcher.js`: add a `qdistro.handshake` outbound on `port.connect()` (the first message after a fresh `connectNative`).
- `src/port.js`: expose `onConnected` hook so dispatcher can fire the handshake exactly once per port lifetime.
- Tests: pin handshake retry behavior, expired-token rejection, nonce-replay rejection.

## Bridge counterpart

Tracked in `../../qdistro/doc/browser.md`. The bridge must:

- Accept the handshake op and derive the same shared key.
- Reject every other op until the handshake completes.
- Reject any op whose intent token fails HMAC verification, is expired, or replays a nonce within the TTL window.

The bridge-side gate ships independently — the extension-side mint can land first; the bridge will simply reject anything HMAC-signed until it learns the key. **Do not** delete the `hmacPlaceholder()` short-circuit until the bridge counterpart ships.

## Test seams

- Vitest can drive `crypto.subtle` via Node's `globalThis.crypto` (Node 19+).
- The synthetic `browser` shim doesn't need changes — handshake is just a normal dispatcher.request.
- Cover: fresh-handshake happy path, mid-session bridge restart (tokens rejected → re-handshake → retry), TTL expiry, nonce replay.

## Done = ?

- `intent.mint()` returns `hmac=<base64>` always after handshake.
- Bridge rejects `hmac=null` tokens unconditionally.
- A unit test asserts that after `port._resetForTests()` followed by a fresh `connect()`, the first outbound message on the wire is `qdistro.handshake`.

## See also

- Sibling track in qdchrome-extension: `../../qdchrome-extension/todo/01-intent-tokens.md`. Keep the two implementations behaviorally identical — same curve, same HKDF, same HMAC. Single bridge serves both.
