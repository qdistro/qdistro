# 05 — System-level injection (Chromium)

## Why

qdistro's base image should ship the extension installed and pinned in every Chromium-derivative browser the user opens. This document covers the Chromium-side flow; Firefox lives in qdfirefox-extension's todo dir (`../../qdfirefox-extension/todo/01-system-install-firefox.md`).

## What Chromium supports

### a) Enterprise managed policy (preferred)

Drop a JSON file at one of:

- `/etc/chromium/policies/managed/qdistro.json` (Chromium on Linux)
- `/etc/opt/chrome/policies/managed/qdistro.json` (Google Chrome on Linux)
- `/etc/brave/policies/managed/qdistro.json` (Brave; same Chromium scheme)

```json
{
  "ExtensionInstallForcelist": [
    "<extension-id>;file:///usr/share/qdistro/extensions/qdistro-chrome-update.xml"
  ],
  "ExtensionSettings": {
    "<extension-id>": {
      "installation_mode": "force_installed",
      "update_url": "file:///usr/share/qdistro/extensions/qdistro-chrome-update.xml",
      "toolbar_pin": "force_pinned"
    }
  }
}
```

The `update_url` is an `updates.xml` (Omaha/CRX update protocol) that Chromium polls — point it at a local file URL whose body references the bundled `.crx`.

`installation_mode` options:
- `force_installed` — installed; user cannot disable or uninstall.
- `normal_installed` — installed; user can disable.
- `allowed` / `blocked` — allowlist / blocklist without installing.

### b) System-wide drop-in directory

```
/etc/chromium/external_extensions.json
```

Or per-extension preference files under:

```
/usr/share/chromium-browser/extensions/<extension-id>.json
{
  "external_crx": "/usr/share/qdistro/extensions/qdistro-chrome.crx",
  "external_version": "0.2.0"
}
```

## Stable extension ID — required up front

Chromium's extension ID is the SHA-256 prefix of the public key in `manifest.key`. Without a fixed `key`, every build produces a different ID and policy targeting breaks.

**Action item before any forcelist policy works**: generate a keypair once and bake the public key into `manifest.chromium.json`:

```bash
# Generate key (once); store qdistro.pem in a secure secret store.
openssl genrsa -out qdistro.pem 2048

# Derive public-key field for manifest.
openssl rsa -in qdistro.pem -pubout -outform DER | base64 -w0
```

Add the base64 string as `"key": "<base64>"` in `manifest.chromium.json`. The resulting extension ID stays constant across rebuilds.

## The signing wall

Self-signed crxs don't install on stable Chromium without enterprise policy. Three options:

| Path | Effect |
|---|---|
| **Chrome Web Store** | Submit to CWS; signed crx installs through normal flow. Web Store review cycle applies. |
| **Self-distributed crx + enterprise policy** | Pack with `chromium --pack-extension=dist/chromium --pack-extension-key=qdistro.pem`. Policy at `/etc/chromium/policies/managed/qdistro.json` force-installs it. Works on Chromium and Chrome wherever the policy is in place. |
| **Developer-mode unpacked** | `chromium --load-extension=...` per launch. Not viable for OS-level injection — it's a CLI flag, not a policy. |

The self-distributed crx path is the sweet spot for qdistro.

## Concrete deliverables

1. **Stable extension key** — add `"key"` to `manifest.chromium.json`. Keep `qdistro.pem` in a secure store; it's also needed to sign updates.

2. **`scripts/pack-crx.sh`** — runs `chromium --pack-extension=dist/chromium --pack-extension-key=qdistro.pem`, emits `dist/qdistro-chrome.crx`.

3. **`scripts/build-update-xml.sh`** — writes the Omaha-style `dist/qdistro-chrome-update.xml` whose `<updatecheck>` points at the bundled crx and whose version matches the manifest.

4. **`scripts/install-system-policy.sh`** — writes `/etc/chromium/policies/managed/qdistro.json` (merging with existing managed-policy JSON via `jq`). Mirror the Firefox script in `../../qdfirefox-extension/scripts/`.

5. **Native-host system path** — `scripts/build-extension.sh` already covers user install. Extend `scripts/install-native-host.sh` (or add one if missing) with `--system` flag that writes:
   - `/etc/chromium/native-messaging-hosts/qdistro.json` and
   - `/etc/opt/chrome/native-messaging-hosts/qdistro.json`
   - manifest field is `allowed_origins` with `chrome-extension://<extension-id>/` URIs (not Firefox's `allowed_extensions`).

6. **Image-pipeline integration** — drop an rpm/deb into the qdistro baseweed image build that places:
   ```
   /usr/share/qdistro/extensions/qdistro-chrome.crx
   /usr/share/qdistro/extensions/qdistro-chrome-update.xml
   /etc/chromium/policies/managed/qdistro.json
   /etc/chromium/native-messaging-hosts/qdistro.json
   ```

## Verification (post-install)

```bash
# Policy is in effect
chromium --headless --dump-dom 'chrome://policy' 2>/dev/null | grep qdistro-chrome

# Extension is installed
ls ~/.config/chromium/Default/Extensions/<extension-id>/

# Native host wired
test -f /etc/chromium/native-messaging-hosts/qdistro.json
```

## Multi-browser story

Most qdistro users will install just one browser. The system policy files for Firefox and Chromium are independent; shipping both is non-conflicting. A meta-package `qdistro-browser-bridge` can pull in `qdistro-chromium-extension` and `qdistro-firefox-extension` as subpackages; the user gets the bridge in whichever browser they actually open.

## See also

- `../../qdfirefox-extension/todo/01-system-install-firefox.md` — symmetric flow for Firefox.
- Chromium policy templates: https://chromeenterprise.google/policies/
- Chrome External Extensions guide: https://developer.chrome.com/docs/extensions/how-to/distribute/install-extensions
