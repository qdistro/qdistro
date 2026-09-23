# 03 — pwd autofill content-script

## Status: in progress

Initial content-script lands in commit alongside this doc. See `src/content/pwd-content.js`. Open items below.

## What ships now

- `src/content/pwd-content.js` injects into `<all_urls>` at `document_idle`.
- Listens for `focus` on `<input type="password">`. On focus, sends `{kind: "pwd.request_fill", url, username?}` to background. Background mints an intent token, calls `qdistroPwd.fill`, and posts the reply back to the content script.
- Reply `{credentials: [{username, password}, ...]}` from the bridge → if exactly one credential matches, fill it directly; otherwise the content script renders a minimal credential picker overlay positioned next to the focused input.
- Listens for `submit` on the surrounding `<form>`. If the password value differs from the last filled value (or no fill ran), send `{kind: "pwd.request_save", url, username, password}` to background; background mints a token and calls `qdistroPwd.save`.

## Open items

1. **Cross-frame**. **Resolved 2026-05-16.** `pwd-content.js` now injects with `all_frames: true` (split into its own `content_scripts` entry so mpris/screenlock stay top-frame-only). Federated SSO flows inside iframes are now reached. Each frame uses its own `location.href` for the credential lookup — that's the correct security boundary, since saved credentials are keyed by the iframe's origin, not the embedder's. If a real-world site shows perf regressions from injection into many ad/tracker iframes, fall back to the options-page allowlist tracked in [02 of qdchrome's todo].

2. **Credential-picker UI**. The MVP overlay is functional but ugly: a fixed-position `<div>` styled inline, no keyboard navigation, no escape-on-blur. Replace with a shadow-DOM widget styled to match Firefox's own login-doorhanger.

3. **Save-prompt UX**. Currently silent — the save fires without asking. Should at minimum show a Firefox-native confirmation (via the notifications API) before sending; ideally a doorhanger anchored to the address bar (requires the page-action API and a per-tab state machine).

4. **Phishing surface**. The content script reveals the bridge's existence to every page (via `runtime.sendMessage` traffic visible from devtools). That's intentional for autofill UX but document the threat model: a malicious page can't read the bridge's reply (cross-origin), but it can spoof a login form to attract fills. Mitigation: never fill on `<input>`s that aren't inside a form whose action is same-origin with the URL the credential was saved for. Implement before any real production use.

5. **Test coverage**. The vitest harness doesn't load content scripts (`tests/helpers.js` evals background modules only). Need a parallel `loadContent()` harness that synthesizes a DOM via `happy-dom` and attaches the script. Tracked in [07-ui-tests.md](07-ui-tests.md).

## Wire surface

| Direction | Op (sendMessage `kind`) | Body |
|---|---|---|
| content → bg | `pwd.request_fill` | `{url, username?}` |
| content → bg | `pwd.request_save` | `{url, username, password}` |
| bg → content | `pwd.fill_result` | `{credentials: [...]}` |

Background-side dispatch in `src/background.js` mints the intent token before calling `qdistroPwd.fill` / `qdistroPwd.save`.

## See also

- [06-intent-token-hmac.md](06-intent-token-hmac.md) — autofill is one of the highest-stakes intent-token sites; tokens must be HMAC-signed before this ships to users.
