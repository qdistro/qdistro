# 07 — UI/glue coverage (background, popup, options, content scripts)

## Status (2026-05-16)

| File | Coverage |
|------|----------|
| `src/background.js` | `tests/background.test.js` — runtime.onMessage entry points + content-script forward paths (no startup/install events yet) |
| `src/popup.js` | `tests/popup.test.js` (jsdom) — status load, container dropdown, ping, cookies-export with/without container, no-active-tab, settings link, rejection handling |
| `src/options.js` | `tests/options.test.js` (jsdom) — defaults, storage reflection, allowlist parse, save round-trip, Saved-indicator flash, containers checkbox present |
| `src/content/pwd-content.js` | `tests/pwd-content.test.js` (jsdom) — focus on password fires `pwd.request_fill`; single-cred auto-fills + dispatches input/change; multi-cred renders the picker and clicking a row fills + closes; submit-after-unchanged stays silent; submit-after-edit fires `pwd.request_save`; empty password is a no-op |
| `src/content/mpris-content.js` | `tests/mpris-content.test.js` (jsdom) — no-media skips reports; with media, snapshot is sent with metadata-or-document-title fallback; play/pause events force an immediate report; duplicate snapshots are suppressed; inbound `mpris.do_action` for play/pause/seek invokes the right HTMLMediaElement method; next/previous reply `action_unsupported_by_page`; unknown action replies `unknown_action`; no media replies `no_media_element` |
| `src/content/screenlock-content.js` | `tests/screenlock-content.test.js` (jsdom) — fullscreen entry classifies presentation vs video (incl. nested playing video); paused video classifies as presentation; exit after inhibit reports release; exit without inhibit is silent; pagehide releases when active; pagehide is a no-op otherwise; `webkitfullscreenchange` also drives the listener |

## Remaining gap

None of the source files are uncovered now. Iframe-specific tests for pwd-content (top-vs-iframe `location.href`) could still be added but require a multi-document jsdom setup; defer until a regression shows.

## Plan

1. **`happy-dom` for DOM** — lighter than jsdom; vitest supports it as the `environment` option per-file. `// @vitest-environment happy-dom` at the top of each UI test.
2. **Background.js harness extension** — `helpers.js` gets a `loadBackground()` that additionally evals `src/background.js`. The synthetic `browser.runtime.onMessage.addListener` becomes a stub that records the registered listener so tests can fire synthetic `runtime.sendMessage` calls.
3. **Popup/options harness** — `loadPopupDom(htmlPath)`. Reads the HTML, parses into happy-dom, then evals `popup.js` against the resulting `document`. Tests assert on `document.getElementById("status").textContent` etc.
4. **Content-script harness** — same shape, but the synthetic `browser.runtime.sendMessage` records outbound messages so tests can assert what the content script tried to send the background.

## What to cover, in priority order

| File | Cases |
|---|---|
| `background.js` | sender-id check rejects foreign senders; `status` returns connected/disconnected; `ping` routes through dispatcher; `cookies.export` mints intent + scopes to `cookie_store_id`; `containers.list` returns shape; content-script `pwd.request_fill` mints + forwards |
| `popup.js` | status renders connected/disconnected with correct color class; Ping button triggers send; Container picker populates from `contextualIdentities.query`; cookies-export passes the picked store id |
| `options.js` | load from storage hydrates checkboxes + textarea; save persists shape `{modules, origin_allowlist}` |
| `content/pwd-content.js` | password-input focus fires `pwd.request_fill`; reply with single credential fills value; reply with multiple shows picker overlay; form submit fires `pwd.request_save` only when password changed |
| `content/mpris-content.js` | metadata change fires `mpris.report_update`; bridge-side `mpris.do_action` invokes mediaSession action |
| `content/screenlock-content.js` | `fullscreenchange` to fullscreen fires `screenlock.report_inhibit`; back to non-fullscreen fires `report_release`; `pagehide` fires release |

## Non-goals for this track

- Visual regression / pixel diff. That's the integration corpus, not vitest.
- Real Firefox process startup. `web-ext run` is for the integration corpus.
- Coverage of `tests/integration/firefox-gui/*.md` — those are agent-driven and have their own assertion model.

## See also

- Sibling track at `../../qdchrome-extension/todo/04-ui-tests.md` — same gap, mirrored fix. Whichever lands first should publish the harness extension; the other repo can copy it.
