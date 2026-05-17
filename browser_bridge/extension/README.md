# qdistro browser bridge — WebExtension ( MVP)

Dual-manifest source. One codebase serves both:

- **Firefox** — `manifest.firefox.json` (MV2; renamed to `manifest.json`
 at packaging time).
- **Chromium / Chrome / Brave / Vivaldi / Edge** — `manifest.chromium.json`
 (MV3 with empty service worker; the popup owns the round-trip).

`build-extension.sh` packages both shapes side-by-side into
`dist/qdistro-firefox.xpi` (unsigned — see spec/14
§"Distribution constraints"; AMO sign is a release-time step) and
`dist/qdistro-chromium.zip` (loadable as an unpacked extension or
packaged into a CRX with the qdistro signing key).

## Loading for development

### Firefox

 about:debugging → This Firefox → Load Temporary Add-on… →
 pick `manifest.json` from `dist/firefox/`.

The native-messaging manifest at
`~/.mozilla/native-messaging-hosts/qdistro.json` must be in place
already (run `qdistro-browser-install --browsers firefox`).

### Chromium / Chrome / Brave / Vivaldi / Edge

 chrome://extensions → Enable Developer mode → Load unpacked →
 pick `dist/chromium/`.

The matching manifest must be in the per-browser
`NativeMessagingHosts/` directory (run `qdistro-browser-install`
without `--browsers` to install all six).

## What the popup does

1. Click "qdistro.ping" button.
2. Extension runs `runtime.connectNative("qdistro")` — browser
 spawns the bridge from
 `/usr/lib/qdistro/browser-bridge`.
3. Sends `{op: 'qdistro.ping', echo: <ts>, extension_id: <runtime.id>}`.
4. Bridge round-trips with `{pong: true, ppid, parent_exe,
 parent_selinux, extension_id, echo, op}`.
5. Popup renders the JSON in the `<pre>` block.

If the bridge's parent-exe identity check fails (e.g. a Snap
Firefox build hits the bridge through `xdg-desktop-portal`), the
bridge replies `{ok: false, error: "parent_not_allowed", parent_exe}`.
The popup shows that body verbatim. Sandboxed-browser rejection
is the documented behaviour — see spec/14
§"Supported-browser matrix".
