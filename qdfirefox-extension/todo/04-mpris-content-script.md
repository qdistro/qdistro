# 04 — mpris content-script (Media Session)

## Status: in progress

Initial content-script lands alongside this doc. See `src/content/mpris-content.js`. Open items below.

## What ships now

- `src/content/mpris-content.js` injects into `<all_urls>` at `document_idle`.
- Observes `navigator.mediaSession.metadata` and `navigator.mediaSession.playbackState` changes via a 1Hz poll (Media Session API has no event for metadata updates; polling is the only portable option). Reports `{title, artist, album, art_url, state}` to background via `runtime.sendMessage` with `kind: "mpris.report_update"`.
- Background forwards as `mpris.update` to the bridge via `qdistroMpris.update`. The bridge re-exposes the metadata on the D-Bus MPRIS bus for compositor / shell consumption.
- For the inbound direction (bridge → extension `mpris.control`): the existing dispatcher handler in `src/modules/mpris.js` was a stub returning `{ok:true, stub:true}`. It now finds the most recently active media tab, asks its content script to invoke `navigator.mediaSession.actions.<action>` if registered (else falls back to `HTMLMediaElement.play()/.pause()` on the first `<audio>` / `<video>` in the document).

## Open items

1. **Polling is wasteful**. 1Hz across every tab is cheap individually but adds up. Throttle: poll only when at least one media element exists on the page (`document.querySelector('audio,video')`), or hook `play`/`pause`/`ended` events to drive an event-based update and stop the poll loop while paused. Profile before optimizing.

2. **Cross-frame**. YouTube embeds, SoundCloud iframes, etc. live in subframes. The content script needs `all_frames: true` and a frame-id discriminator so the bridge doesn't see duplicate "now playing" entries from the top frame and the iframe both. Pick the frame whose `mediaSession.playbackState === "playing"` and ignore others.

3. **Multi-tab arbitration**. If two tabs are playing audio simultaneously, the bridge gets two `mpris.update` streams. The compositor's MPRIS bus exposes both as separate players (correct) but the qdshell media widget needs to pick one to display. That's a qdshell decision, but the extension should at least flag which tab is `tabs.active === true` so the shell can prefer the active tab.

4. **Album art URLs**. `mediaSession.metadata.artwork[0].src` is a data URL or origin-restricted URL. We forward it verbatim; the compositor would need a fetch through the extension's host_permissions to load it. Alternative: fetch the artwork in the content script, encode as data URL, send to bridge. Bigger payload, simpler downstream.

5. **Test coverage**. Same shape as [03] — needs a DOM-aware content-script harness.

## Wire surface

| Direction | Op (sendMessage `kind`) | Body |
|---|---|---|
| content → bg | `mpris.report_update` | `{title, artist, album, art_url, state, position?, duration?}` |
| bg → content | `mpris.do_action` | `{action: "play" \| "pause" \| "next" \| "previous" \| "seek", value?}` |

Background-side translation handled in `src/background.js`'s onMessage listener and the existing `qdistroMpris` dispatcher hooks.
