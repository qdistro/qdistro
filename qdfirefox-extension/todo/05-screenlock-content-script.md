# 05 — screenlock content-script (fullscreen observer)

## Status: in progress

Initial content-script lands alongside this doc. See `src/content/screenlock-content.js`. Open items below.

## What ships now

- `src/content/screenlock-content.js` injects into `<all_urls>` at `document_idle`.
- Listens for `fullscreenchange` on the document. On entering fullscreen, sends `{kind: "screenlock.report_inhibit", reason}` to background. On exiting, sends `{kind: "screenlock.report_release", reason}`.
- Reason classification:
  - `"fullscreen_video"` — fullscreen element contains a playing `<video>`.
  - `"fullscreen_presentation"` — fullscreen element is anything else (slides, games, etc.).
- Background forwards via `qdistroScreenlock.inhibit` / `qdistroScreenlock.release` to the bridge.
- On `tab.onRemoved` / `pagehide`: the content script sends a `release` so a closed tab doesn't leave the compositor with a stuck inhibit. Background also tracks per-tab inhibit state and emits a `release` if a tab vanishes without the content script getting a `pagehide`.

## Open items

1. **Multi-tab inhibit accounting**. The bridge gets one `inhibit` per fullscreen-tab and one `release` per exit. If tab A is fullscreen and tab B enters fullscreen too, the bridge sees two inhibits — the compositor must reference-count, not toggle. Document the contract clearly in the bridge spec, since "inhibit" naturally reads as a boolean.

2. **Picture-in-picture**. PiP isn't fullscreen but should arguably also inhibit the screen lock. Listen for `enterpictureinpicture` / `leavepictureinpicture` and treat the same.

3. **Wake lock API**. Modern pages call `navigator.wakeLock.request("screen")` directly to inhibit lock. We can intercept by monkey-patching `navigator.wakeLock` in a `MAIN`-world script (Firefox MV3 supports `world: "MAIN"` in `scripting.registerContentScripts`) and forward the request through the bridge instead of the platform. Whether that's desirable is a policy call — the bridge could enforce a per-origin policy on wake locks the same way it does on notifications.

4. **Test coverage**. Same shape as [03], [04] — needs a DOM-aware content-script harness, plus a `fullscreenchange` event mock.

## Wire surface

| Direction | Op (sendMessage `kind`) | Body |
|---|---|---|
| content → bg | `screenlock.report_inhibit` | `{reason: "fullscreen_video" \| "fullscreen_presentation", tab_url}` |
| content → bg | `screenlock.report_release` | `{reason: "fullscreen_exit" \| "tab_unload"}` |

Background-side tab accounting in `src/background.js` ensures that browser-close / tab-close emits a `release` for any tab that had an active inhibit.

## See also

- The Wayland idle-inhibit-unstable-v1 protocol is the compositor side. qdwin exposes it; this content script feeds it.
