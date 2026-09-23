# qdchrome-extension

Chromium MV3 WebExtension and native-messaging client for the qdistro browser
bridge. It is the Chromium-family peer of
[qdfirefox-extension](../qdfirefox-extension): same bridge contract, but without
Firefox containers/contextual identities.

## Role in qdistro

qdistro treats browsers as silo-facing applications, not as a trusted control
plane. This extension is the browser-side adapter that lets the qdistro browser
bridge coordinate tabs, password-vault prompts, page extraction, media status,
downloads, notifications, and screen-lock inhibition for Chromium-based
browsers.

It is not a replacement for [qdbrowser](../qdbrowser). qdbrowser is the
first-party Qt browser with direct qdistro integration. qdchrome-extension is
for Chromium/Chrome when compatibility with the upstream browser engine is
needed.

## Status

v0.2.0. The extension includes bridge modules for tabs, password fill/save,
page extraction, cookie export, MPRIS/media status, downloads, notifications,
and screen-lock inhibition. Tests are Vitest-based and run against synthetic
browser APIs.

Security-sensitive flows are still being aligned with the current qdistro
security model. In particular, password fill and cookie export should be treated
as privileged bridge operations that require trusted UI/bridge policy before any
user-facing install ships.

## Build

```bash
npm install
npm run build
```

The build script writes unpacked browser trees under `dist/`.

## Test

```bash
npm test              # sibling-repo drift check skips-with-warning if absent
npm run test:release  # QDISTRO_REQUIRE_SIBLING=1 — absent sibling is FATAL
```

**Release CI must use `npm run test:release`** (or otherwise set
`$QDISTRO_REQUIRE_SIBLING=1`) with `qdfirefox-extension` checked out
side-by-side, or with `$QDISTRO_SIBLING_GOLDEN` pointing at its
`tests/fixtures/golden-frames.js`. Both repos carry a byte-identical copy of
that fixture — the bridge wire-protocol contract — and
`tests/golden-frames-drift.test.js` warns and exits green instead of comparing
across repos when the sibling is missing, so a plain `npm test` in a single-repo clone can
be green without ever checking that the two protocol copies agree. qdistro's
`qci` host gate sets both env vars for this repo.

## Install

**v1 ships no signed distribution channel for this extension** — no CRX
signing key in production, no hosted `update.xml`, no auto-update. The v1
install is a developer-mode unpacked load, and the operator-facing procedure
(plus what the missing signature and update channel actually cost you) is
[qdistro/doc/browser-extension-install.md](../doc/browser-extension-install.md).
The short version:

```bash
bash scripts/build-extension.sh          # -> dist/chromium/

# On a qdistro install (writes ~/.config/chromium/NativeMessagingHosts/qdistro.json):
qdistro-browser-install --browsers chromium
# From a checkout:
python3 ../browser_bridge/qdistro_browser_install.py --browsers chromium
```

Then load the unpacked extension from `dist/chromium/` in
`chrome://extensions` or `chromium://extensions` with developer mode enabled.
Because `manifest.chromium.json` pins the public key, the unpacked load gets
the same stable id (`ammgnkddbnjdhikklpljgiclldedgncf`) the native-messaging
manifest authorizes.

`scripts/install-system-policy.sh` writes the Chromium enterprise policy that
force-installs a *packed* extension from
`/usr/share/qdistro/extensions/…`. Nothing populates that path in v1 — the
script is scaffolding for the post-v1 signed channel, not a v1 install path.

## Permissions

The extension is a bridge adapter; its permission set is pinned to the
minimal set the ops implemented in `src/` actually use, with a closed-set
test in `tests/manifest.test.js`. That is broader than the *effective* v1
bridge surface under decision D5 (`qdistro.ping`; `containers.*` is Firefox
only) — the module code and the bridge dispatch table still carry the
Phase-9 handlers. See `../doc/browser.md` (P0-4/5/6 disposition):

| Permission | Why |
| --- | --- |
| `nativeMessaging` | Talk to the qdistro browser bridge |
| `tabs` | Enumerate and operate on browser tabs across windows |
| `cookies` | Export cookies through a gated bridge operation |
| `downloads` | Report download lifecycle updates |
| `notifications` | Show notifications requested by the bridge |
| `contextMenus` | Provide "Send to qdistro..." style actions |
| `scripting`, `<all_urls>` | Page extraction and content observers |
| `storage` | Options page state |

`activeTab` (redundant with the `<all_urls>` host grant + `tabs`) and
`webNavigation` (no navigation listener exists in `src/`) were dropped
under S8 P0-5. New permissions require updating the closed-set test after a
security review.

## Architecture

```
qdchrome-extension/
├── manifest.chromium.json
├── src/
│   ├── api.js                   # browser/chrome binding layer
│   ├── port.js                  # native-messaging connection
│   ├── dispatcher.js            # request_id-correlated dispatch
│   ├── intent.js                # short-lived privileged-op token
│   ├── background.js            # MV3 service worker
│   ├── popup.html/js
│   ├── options.html/js
│   ├── content/
│   │   ├── pwd-content.js
│   │   ├── mpris-content.js
│   │   └── screenlock-content.js
│   └── modules/
│       ├── tabs.js
│       ├── pwd.js
│       ├── pageExtract.js
│       ├── cookies.js
│       ├── mpris.js
│       ├── downloads.js
│       ├── notifications.js
│       └── screenlock.js
├── scripts/
│   ├── build-extension.sh
│   └── install-system-policy.sh
└── tests/
```

## Chromium-only — no Firefox build here

This repo builds the Chromium-family extension only. It used to also emit a
Firefox MV2 `dist/firefox.xpi` under gecko id `qdistro@qdistro.local`, but that
collided with the **bundled** Firefox extension that used to ship from
`../browser_bridge/extension` (a different codebase under the *same*
id). To canonicalize the Firefox artifacts, that target was removed.

**For Firefox, build and load [qdfirefox-extension](../qdfirefox-extension)**
(id `qdistro-firefox@qdistro.local`, MV3, first-class containers), with
`qdistro-browser-install --browsers firefox`.

That bundled tree has since been **deleted** (J11): it was an abandoned fork
that never grew the module/origin gate, so it had no origin allowlist at all,
and it was the only extension the qdistro installer actually laid down. Its
id `qdistro@qdistro.local` is now **revoked** — the qdistro bridge refuses it
— and `--firefox-mode bundled` is a hard error, leaving `standalone` as the
only mode. See `../doc/browser-extension-install.md` for the v1
procedure and `../doc/browser.md` ("Firefox extension artifacts").

## Related repos

- [qdistro](../README.md) contains the native browser bridge daemon and the
  architecture/security docs.
- [qdfirefox-extension](../qdfirefox-extension) is the Firefox-native extension
  with contextual-identity support.
- [qdbrowser](../qdbrowser) is the first-party Qt browser.
