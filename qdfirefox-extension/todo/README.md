# qdfirefox-extension — open work

This directory tracks deferred items in qdfirefox-extension. Sibling list at `../../qdchrome-extension/todo/`; items that exist in both repos share a track name and are kept in sync deliberately.

| File | Track | Scope |
|------|-------|-------|
| [01-system-install-firefox.md](01-system-install-firefox.md) | OS-level injection (Firefox) | `policies.json` ExtensionSettings, system native-host, signed-xpi staging, signing wall |
| [02-system-install-chromium.md](02-system-install-chromium.md) | OS-level injection (Chromium) | `ExtensionInstallForcelist` policy, system native-host — applies to **qdchrome-extension**, listed here for symmetry |
| [03-pwd-content-script.md](03-pwd-content-script.md) | Autofill wiring | `<input type=password>` focus detection, fill-on-pick, save-on-submit |
| [04-mpris-content-script.md](04-mpris-content-script.md) | Media observer | Web Media Session API listener; reports playback state to bridge |
| [05-screenlock-content-script.md](05-screenlock-content-script.md) | Fullscreen observer | `fullscreenchange` listener → screenlock.inhibit/release |
| [06-intent-token-hmac.md](06-intent-token-hmac.md) | Real intent tokens | `crypto.subtle` HMAC, `qdistro.handshake` round-trip, TTL+nonce store |
| [07-ui-tests.md](07-ui-tests.md) | UI/glue coverage | jsdom tests for background.js / popup.js / options.js |
| [08-bridge-protocol-alignment.md](08-bridge-protocol-alignment.md) | Bridge protocol audit | Discovered + landed alignment with `qdistro_browser_bridge.py` (token shape, op renames, handshake). Partly done. |

`page.extract.request` usage is product documentation now:
`../../qdistro/doc/browser-page-extract.md`.

## Status snapshot (as of 2026-05-16)

- v0.2.0 ships 9 modules and 59 vitest cases; all green.
- Integration scenario 01 (popup-connects-to-stub) verified end-to-end against real Firefox 150 via journal lines; visual asserts blocked on VM tooling gap (separate memory `project-vm-gui-tooling-gap`).
- Three modules are dispatcher-only (`pwd`, `mpris`, `screenlock`) — their content-script observers are tracked in [03], [04], [05].
- Intent tokens carry `hmac=null`; the bridge daemon is the security gate until [06] lands.

## Re-read cadence

- After any bridge-side handler ships (especially the `qdistro.handshake` op).
- After the first AMO submission (informs the signing-wall workaround in [01]).
- Six months without a re-read.
