# 07 — Bridge protocol alignment

## Discovered 2026-05-16

Auditing `qdistro/browser_bridge/qdistro_browser_bridge.py` showed real divergence between what the extension emits and what the bridge accepts. The bridge is more developed (600+ LOC, real D-Bus integration, working tests in `qdistro/tests/unit/test_browser_bridge_phase9.py`) and is the source of truth.

## Mismatches

### Intent token shape (load-bearing — without alignment HMAC fails)

| | Extension mints | Bridge `verify_intent_token` expects |
|---|---|---|
| id | `nonce: "<counter>-<rand>"` | `request_id: "<str>"` |
| time | `timestamp: <ms>` | `ts: <seconds>` |
| op | `operation: "<op>"` | `op: "<op>"` |
| canonical | `?` | `request_id\|ts\|op` |
| hmac algo | unspecified | HMAC-SHA256 hex |
| ttl | `ttl_ms: 5000` | bridge constant `INTENT_TOKEN_TTL_S` |
| extra | `hmac: null` (stub) | rejects null |

The bridge's `qdistro.handshake` op already exists: returns `{session_secret_hex, token_ttl_s, hmac_algo, token_canonical}`. Extension just needs to call it on connect, store the secret, mint HMACed tokens with the bridge's field names.

### Wire op names

| Extension sends | Bridge expects | Resolution |
|---|---|---|
| `mpris.update` | `mpris.publish` | Rename in extension |
| `downloads.update` | `downloads.notify` | Rename in extension |
| `notifications.event` | (no handler) | **Dropped 2026-05-16** — extension no longer emits it. Re-add if bridge ever ships a handler. |

### Field renames within those ops

**mpris.publish**: bridge `_forward` whitelists `(title, artist, album, playback_status, position_us, tab_id)`.

| Extension field | Bridge field | Notes |
|---|---|---|
| `state` | `playback_status` | rename |
| `position` (seconds) | `position_us` (microseconds) | multiply by 1_000_000 |
| `art_url`, `duration`, `url` | — | extras; bridge ignores |

**downloads.notify**: bridge `_forward` whitelists `(download_id, filename, state, bytes_received, total_bytes, url, mime)`.

| Extension field | Bridge field | Notes |
|---|---|---|
| `id` | `download_id` | rename |
| `start_time` | — | extra; bridge ignores |

### Direction confusion

The bridge has `_handle_notifications_show` as if the extension sends `notifications.show` to be relayed to the compositor's notification bus. The extension treats `notifications.show` as bridge-initiated. The actual gap: there's no extension-initiated path for page-Notification-API events. Solution requires a content-script Notification polyfill — deferred.

The existing extension behavior (inbound `notifications.show` from the bridge → `browser.notifications.create`) is independently useful (lets the bridge tell the extension to show a system notification) so keep it; the bridge handler is for a different code path that no caller currently exercises.

### Bridge-side gaps the extensions assume work

- `containers.list / .create / .remove` — bridge has no handlers. Extension code is dead until the bridge ships them (qdfirefox-extension is the only emitter; qdchrome-extension doesn't have a containers module).
- `cookies.export` `cookie_store_id` field — bridge's `_handle_cookies_export` ignores unknown fields. Forward-compatible.

## Plan

1. **Extension intent.js rewrite** — async mint, HMAC-SHA256 via `crypto.subtle`, fields `{request_id, ts, op, hmac}`. Bridge canonical `request_id|ts|op`. Lands in both repos.

2. **Handshake on connect** — port.js `connect()` fires `qdistro.handshake` immediately after the port is up; awaits the reply, stores `session_secret_hex` for intent.js. Re-runs on every reconnect (bridge rotates secret on its restart).

3. **Op renames** — `mpris.update` → `mpris.publish`, `downloads.update` → `downloads.notify`. Field renames per the tables above. Tests updated.

4. **Drop `notifications.event`** — **Done 2026-05-16.** Listeners removed from `src/modules/notifications.js`; tests pinned to confirm no outbound emission on click/close. Future bridge expansion can re-add a `notifications.event` op and rewire the listeners.

5. **Tests** — re-pin every changed wire shape. Both repos.

6. **Bridge tracks** (out of scope of the extension repos; documented here for visibility):
   - Add `containers.list/.create/.remove` handlers if compositor wants Firefox container awareness.
   - Add `notifications.event` if browser-internal notification click/close should reach the bridge.

## Done = ?

- An extension call to `cookies.export` with a freshly-minted token passes the bridge's `verify_intent_token` against a live bridge.
- `mpris.publish` and `downloads.notify` reach the bridge's `_forward` paths with the field names the bridge's `_forward` whitelist accepts.
- `qdistro.handshake` is the first message the extension sends on every (re)connect.
- All existing tests updated; the suite stays green.

This blocks [01-intent-tokens.md](01-intent-tokens.md) (which is now a subset of this task) and any production install of the extension.
