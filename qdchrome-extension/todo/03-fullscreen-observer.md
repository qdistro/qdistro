# Screenlock fullscreen observer

`src/modules/screenlock.js` exposes `inhibit()` / `release()`
primitives that forward to the bridge as `screenlock.inhibit` /
`screenlock.release`. There is no observer that calls them. The
content-script wiring is deferred per the source comment.

## Why it matters

Compositor screen-lock is the user-facing surface for "I'm watching
a video, don't blank the screen." Today qdbrowser-rendered content
that goes fullscreen does not inhibit the lock; the user notices
after their first 2 a.m. movie that the screen darkens mid-scene.
The bridge already routes `screenlock.inhibit` to the qdwin
compositor; the extension just doesn't fire it.

## Deliverables

### 1. Content script

`src/content/screenlock.js` (new directory). Registered in the
manifest with `"matches": ["<all_urls>"]`, `"run_at":
"document_start"`. Hooks:

```js
document.addEventListener("fullscreenchange", () => {
  const fs = document.fullscreenElement;
  chrome.runtime.sendMessage({
    op: "screenlock.local.fullscreen",
    state: fs ? "enter" : "exit",
    tab_url: location.href,
  });
});
```

A `<video>` that goes fullscreen via the Picture-in-Picture path
(`requestPictureInPicture`) is not caught by `fullscreenchange`;
listen for `enterpictureinpicture` / `leavepictureinpicture` on
`HTMLVideoElement.prototype` (delegate via document-level
listener since elements are dynamic).

### 2. Service-worker accounting

`src/modules/screenlock.js` keeps a per-tab `Set` of "active inhibit"
flags. The bridge contract is one inhibit/release pair per source,
so the module manages refcounting:

- First `fullscreen.enter` for tab T → send `screenlock.inhibit`.
- Subsequent `enter` events from same tab → no-op.
- `exit` → if last active inhibit for the tab, send
  `screenlock.release`.
- Tab close (`chrome.tabs.onRemoved`) → if tab had an active
  inhibit, send `screenlock.release` (cleanup).
- Port reconnect after a service-worker suspend cycle → if any tab
  is still flagged, re-send `screenlock.inhibit` to re-establish
  bridge-side state. The bridge is idempotent here (track 9e of
  the bridge spec).

### 3. Per-origin policy

Same posture as the notifications allowlist (see track 02 here): an
origin can be denied the right to inhibit. Default: allow. Storage:
`chrome.storage.local.screenlock.allowlist`. Useful when a misbehaving
SPA tries to inhibit the lock for ad-rendering purposes.

## Tests

- Extend `tests/screenlock.test.js`:
  - `fullscreenchange` event from a tab fires `screenlock.inhibit`.
  - Second `enter` from same tab → no second inhibit.
  - `exit` fires `screenlock.release`.
  - Tab-close with an active inhibit fires release.
  - Allowlist denial → no inhibit, journal line logged.
- New `tests/content_screenlock.test.js` — content-script harness
  (mock `document.addEventListener` + `chrome.runtime.sendMessage`)
  verifying the listener wiring.

## Acceptance criteria

1. YouTube fullscreen → bridge gets `screenlock.inhibit` →
   qdwin no longer blanks → user can watch the movie.
2. ESC out of fullscreen → bridge gets `screenlock.release` →
   normal idle-blanking resumes.
3. Tab crash with active fullscreen → bridge eventually gets a
   release (via the tab-removed cleanup, or via the bridge's own
   heartbeat-timeout cleanup if the service worker died with
   the tab).
