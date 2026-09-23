# UI / orchestration test coverage

## Status (2026-05-16)

| File | LOC | Coverage |
|------|-----|----------|
| `src/background.js` | 224 | `tests/background.test.js` — runtime.onMessage entry points (sender check, pwd/mpris/screenlock content-script forwards) |
| `src/popup.js` | 65  | `tests/popup.test.js` (jsdom) — load-time status, ping, cookies-export, no-active-tab, settings link, null-response handling |
| `src/options.js` | 53 | `tests/options.test.js` (jsdom) — defaults, storage reflection, allowlist parse, save round-trip, Saved-indicator flash |
| `src/content/pwd-content.js` | 178 | `tests/pwd-content.test.js` (jsdom) — focus fires `pwd.request_fill`; single-cred auto-fills + dispatches input/change; multi-cred renders the picker; submit-after-unchanged stays silent; submit-after-edit fires `pwd.request_save` |
| `src/content/mpris-content.js` | 142 | `tests/mpris-content.test.js` (jsdom) — no-media skip; with-media report with metadata fallback; play/pause forces report; dup suppression; inbound do_action for play/pause/seek/next/previous/unknown |
| `src/content/screenlock-content.js` | 73 | `tests/screenlock-content.test.js` (jsdom) — fullscreen entry classification (video/presentation, nested, paused); exit reports release; pagehide release; webkit-prefixed event |

`background.js` startup paths (`runtime.onStartup` / `onInstalled`) are still untested — the `loadWithBackground` harness only fires `runtime.onMessage`. A follow-up could synthesize the startup events and assert the persistent `connectNative` opens + reconnect-with-backoff fires.

The existing harness (`tests/helpers.js`) loads source via
`new Function(...)` against a fake `self` global. That works for
modules that are pure logic and chrome-API shims. It breaks for
these three files because:

- `background.js` registers `chrome.runtime.onStartup` /
  `onInstalled` listeners and only does work in their callbacks;
  the test would have to fire them synthetically (manageable) and
  then assert on side effects across all twelve already-loaded
  module exports (harder).
- `popup.js` and `options.js` are DOM-driven: they query
  `document.getElementById`, attach click handlers, mutate
  innerText. The current harness has no DOM.

## Approach

### 1. Add jsdom (optional dev-dep)

```bash
npm i -D jsdom
```

`vitest` already supports `environment: "jsdom"` per-test. Use a
`/** @vitest-environment jsdom */` pragma at the top of each new test
file so the rest of the suite stays in node-env.

### 2. background.js smoke test

`tests/background.test.js`:

- Load `background.js` after the rest, with `chrome.runtime.onStartup`
  / `onInstalled` events captured.
- Fire `onStartup`. Assert the persistent port opened
  (`chrome.runtime.connectNative` called with `"qdistro"`).
- Fire `port.disconnect`. Assert the reconnect-with-backoff fires
  (use `vi.useFakeTimers()` to advance through 1 s → 2 s → ...).
- Assert each module's activation hook ran in order.

### 3. popup.test.js

`/** @vitest-environment jsdom */` at top.

- `document.body.innerHTML` seeded with a copy of `src/popup.html`.
- Load `popup.js`.
- Click `#ping`. Assert `chrome.runtime.connectNative` round-trip
  fires and `#out` text is the response JSON.
- Click `#export-session`. Assert `qdistroIntent.mint("cookies.export")`
  is called and a `cookies.export` frame is sent.
- Click without bridge available → assert `#status` shows the
  disconnected text and verbatim payload.

### 4. options.test.js

Same jsdom pragma.

- Load with `chrome.storage.local` pre-seeded with sample allowlist.
- Assert the table renders rows for each origin.
- Add a row → assert `chrome.storage.local.set` called with the
  merged map.
- Remove a row → assert `set` called with the row removed.
- TTL display reflects `qdistroIntent.ttlMs()`.

## Acceptance criteria

1. `npm test` runs all 110 existing + ~15 new = ~125 tests, all
   green in under 5 s.
2. v8 coverage (`npx vitest run --coverage`) reports non-zero
   lines for background/popup/options — note that the
   `loadExtension()` harness loads source via `new Function`, which
   v8 doesn't instrument, so the existing 110 tests are not visible
   to coverage. The jsdom tests should import via real `import()`
   so they at least show up.

## Anti-goals

- Don't refactor source to make it more testable. The MV3
  service-worker / popup / options shape is dictated by the
  WebExtension platform; rewriting it to look like an importable
  library would obscure the deployment artefact.
- Don't add Puppeteer or browser-in-VM tests in this track. Those
  belong in the qdistro VM probe (`s67-qdchrome-extension-probe.sh`,
  not yet written) which is a follow-up to track 9b of the bridge
  todo.
