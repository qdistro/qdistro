# qdchrome-extension — open work

The extension is a Phase-8 MVP shape across every module: each one has
the wire contract pinned by tests (110 vitest cases at last count),
but several modules deliberately defer policy enforcement to the
bridge or leave wiring stubbed. This directory documents the
follow-ups.

The bridge counterpart of each item lives in
`../../qdistro/doc/browser.md` (relative to qdistro-org).
These extension tracks unblock the corresponding bridge phases as the
bridge handlers ship.

| File | Track | Scope |
|------|-------|-------|
| [01-intent-tokens.md](01-intent-tokens.md) | Real intent tokens | `crypto.subtle` HMAC, `qdistro.handshake` round-trip, TTL+scope+nonce store |
| [02-policy-enforcement.md](02-policy-enforcement.md) | Extension-side gates | Token mint sites for pwd/pageExtract, per-origin allowlist for notifications |
| [03-fullscreen-observer.md](03-fullscreen-observer.md) | screenlock wiring | Content-script `fullscreenchange` observer, multi-tab inhibit accounting |
| [04-ui-tests.md](04-ui-tests.md) | UI/glue coverage | jsdom smoke tests for background.js / popup.js / options.js (236 LOC currently untested) |
| [05-system-install-chromium.md](05-system-install-chromium.md) | OS-level injection | `ExtensionInstallForcelist` policy, stable manifest `key`, signed crx, system native-host |
| [07-bridge-protocol-alignment.md](07-bridge-protocol-alignment.md) | Bridge protocol audit | Discovered + landed alignment with `qdistro_browser_bridge.py` (token shape, op renames, handshake). |

This repo is Chromium-only. The Firefox extension lives in
`../../qdfirefox-extension` (standalone — the maintained one v1 users load)
and `../../qdistro/browser_bridge/extension` (bundled — a legacy
compatibility artifact with no origin allowlist, J11); the former
Firefox-MV2-via-this-repo track was dropped and its build target removed.

`page.extract.request` usage is product documentation now:
`../../qdistro/doc/browser-page-extract.md`.

## Status snapshot (as of 2026-05-15)

- 12 of 15 source files have behavioural tests through the
  `loadExtension()` harness.
- 3 files have **zero** test contact: `background.js`, `popup.js`,
  `options.js` — the MV3 service-worker boot, the action-popup UI,
  and the options page.
- 5 of the tested modules pin "MVP shape" behaviour that defers
  policy decisions to the bridge. Track 02 here lifts those decisions
  into the extension where the spec requires user-touch attestation
  before the call leaves the browser.

## Re-read cadence

- After any Phase-9 bridge op ships (especially 9d + 9a).
- After the extension grows a second contributor.
- Six months without a re-read.
