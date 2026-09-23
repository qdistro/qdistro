# Extension-side policy enforcement

Three modules currently forward policy decisions to the bridge
without local pre-checks. That's the MVP posture: the bridge is the
trust boundary, and the extension is intentionally thin. But three
specific gates belong in the extension because they require either
user touch (intent token) or per-origin data the bridge cannot see.

## 1. `pwd.fill` / `pwd.save` — intent-token mint at click

**Source:** `src/modules/pwd.js`. The module currently forwards
`intentToken` opaquely from the caller. Today there is no caller
that mints one — `pwd.fill` fires on field focus, `pwd.save` on form
submit, and neither path lands a token.

**Required:** wire token mint to a real user-touch event.

- For `pwd.fill`: the popup-style inline-credential picker (rendered
  by the content script over the focused field) mints a token on the
  user's click on a specific credential entry. Mint scope:
  `pwd.fill`. Send the token in the `pwd.fill` frame as
  `intent_token` (not `intentToken` — match bridge spec).
- For `pwd.save`: the "Save credentials?" prompt (an
  `chrome.notifications.create` action button or the page-action
  popup) mints a token when the user clicks "Save". Scope: `pwd.save`.
- A `pwd.fill` request without a fresh-and-matching token is dropped
  client-side with a log line `pwd.gate refused: no intent token`.
  Bridge-side rejection still applies as defence-in-depth.

Tests in `tests/pwd.test.js` currently pin the opaque-forward
behaviour. Update once this lands; add: "mint happens on
chrome.runtime.onMessage `pwd.user.selected`", "stale token
rejected", "no token → request not sent".

## 2. `page.extract` — token mint moves out of the menu handler

**Source:** `src/modules/pageExtract.js`. Today the context-menu
click handler mints a token and immediately calls
`page.extract`. The programmatic `extract()` entrypoint (used by
keyboard shortcut, command palette, automation) does **not** mint,
so any direct call would be unauthenticated.

**Required:** centralise the mint at the entry to `extract()`. The
context-menu handler should be a thin wrapper that calls
`extract({ source: "context-menu" })`; the keyboard-shortcut handler
calls `extract({ source: "shortcut" })`. Both go through the same
`requireRecentUserGesture()` check — `chrome.permissions.contains`
won't help here; use `chrome.action.onClicked` + a per-tab "last
gesture timestamp" map with a 1 s window.

Tests in `tests/pageExtract.test.js` pin the menu-only behaviour;
update with cases for the shortcut + automation paths.

## 3. `notifications` — per-origin allowlist

**Source:** `src/modules/notifications.js`. The module forwards
notification events with `origin` but does not consult any policy.
The spec puts the gate on the bridge, but the extension is the only
place that has the page's true origin via `chrome.notifications`
metadata; the bridge sees only the forwarded payload.

**Required:** per-origin allowlist consulted before forwarding
`notifications.shown` to the bridge.

- Storage: `chrome.storage.local` under `notifications.allowlist`,
  shape `{ "<origin>": "allow" | "deny" | "prompt" }`. Default
  fall-through: `prompt`.
- `prompt` collapses to drop-and-log until the options-page
  affordance (track 04 here) ships the UI.
- Options page (`src/options.js`) gets an "Notification allowlist"
  table with add/remove/edit rows.
- Audit: every decision logs
  `NOTIFICATIONS_GATE origin=<o> verdict=<v> reason=<r>` via
  `console.info` so the journal forwarder can pick it up.

Tests in `tests/notifications.test.js` pin the unconditional-forward
behaviour; update with allow/deny/prompt cases keyed off
`chrome.storage.local`.

## Anti-goals

- Do **not** move the password-vault lookup into the extension.
  The vault stays on the bridge / daemon; the extension only mints
  the user-touch token that authorises the lookup.
- Do **not** add a UI policy editor for `page.extract` destinations
  in this track — that's track 9c of the bridge spec (share-to
  picker), and it lives in the popup, not the options page.

## Acceptance criteria

1. `pwd.fill` without a fresh user-touch token is dropped before
   leaving the extension; journal line captures the drop.
2. `page.extract` via keyboard shortcut mints a token equivalent to
   the context-menu mint.
3. Notifications from `https://example.org` are dropped when
   `allowlist["https://example.org"] = "deny"`; the bridge never
   sees the frame.
