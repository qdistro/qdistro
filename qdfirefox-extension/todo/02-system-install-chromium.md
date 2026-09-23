# 02 — System-level injection (Chromium)

> The extension targeted here is **qdchrome-extension**, not qdfirefox-extension. This doc lives in qdfirefox-extension's todo dir for symmetry with [01-system-install-firefox.md](01-system-install-firefox.md); the actionable work belongs in `../../qdchrome-extension/todo/`.

## Why

qdistro's base image should land the bridge on whichever browser the user opens. Chromium has its own injection mechanism, similar in spirit to Firefox's enterprise policy but with different paths and a different signing story.

## What Chromium supports

### a) Enterprise managed policy (preferred)

Drop a JSON policy file at:

- `/etc/chromium/policies/managed/qdistro.json` (Chromium on Linux)
- `/etc/opt/chrome/policies/managed/qdistro.json` (Google Chrome on Linux)
- `/etc/brave/policies/managed/qdistro.json` (Brave; same Chromium scheme)

```json
{
  "ExtensionInstallForcelist": [
    "<extension-id>;file:///usr/share/qdistro/extensions/qdistro-chrome.crx"
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

`installation_mode`:
- `force_installed` — installed; user cannot disable or uninstall.
- `normal_installed` — installed; user can disable.
- `allowed` / `blocked` — allowlist / blocklist without installing.

`<extension-id>` is the 32-char Chrome extension ID derived from the public key in the manifest's `key` field. For a self-signed crx, you generate it once with `chrome --pack-extension` and it stays stable. **We need to add a stable `key` to qdchrome-extension's manifest before any forcelist policy can target it.**

### b) System-wide drop-in directory

Per-extension JSON pointer:

```
/etc/chromium/external_extensions.json
```

Or per-extension preference file:

```
/usr/share/chromium-browser/extensions/<extension-id>.json
{
  "external_crx": "/usr/share/qdistro/extensions/qdistro-chrome.crx",
  "external_version": "0.2.0"
}
```

Chromium scans these on launch and installs the referenced crx for every user on the machine.

## The signing wall

Chrome Web Store-signed crxs install anywhere. Self-signed crxs that aren't on the Web Store can only install via enterprise policy on **stable Chromium**. Three options:

| Path | Effect |
|---|---|
| **Chrome Web Store** | Submit qdchrome-extension to CWS; users install signed crx through normal flow. |
| **Self-distributed crx + enterprise policy** | Pack with `chromium --pack-extension=qdchrome-extension/dist/chromium --pack-extension-key=qdistro.pem`. Place under `/usr/share/qdistro/extensions/`. Policy at `/etc/chromium/policies/managed/qdistro.json` force-installs it. Works on Chromium and Chrome with the right policy in place. |
| **Developer-mode unpacked** | `chromium --load-extension=/path/to/unpacked` per Chromium launch. Not viable for OS-level injection because it's a CLI flag, not a policy. |

The self-distributed crx path is the sweet spot for qdistro: no Web Store review cycle, full control over the crx, and works on every Chromium-derivative browser the user might install.

## Concrete deliverables

1. **Stable extension key** in `qdchrome-extension/manifest.chromium.json` — add a `key` field with the base64-encoded SubjectPublicKeyInfo from a generated keypair. Once set, the extension ID is permanent. Keep the private key (`qdistro.pem`) in a secure store; it's also needed to sign updates.

2. **`qdchrome-extension/scripts/pack-crx.sh`** — runs `chromium --pack-extension` against `dist/chromium/` with `qdistro.pem`, emits `dist/qdistro-chrome.crx`.

3. **`qdchrome-extension/scripts/install-system-policy.sh`** — writes `/etc/chromium/policies/managed/qdistro.json` (merging with existing managed-policy JSON via `jq`). Mirror the Firefox script.

4. **Native-host manifest paths for Chromium** — different from Firefox:
   - System: `/etc/chromium/native-messaging-hosts/qdistro.json`, `/etc/opt/chrome/native-messaging-hosts/qdistro.json`
   - Manifest field is `allowed_origins` (chrome-extension:// URIs), not `allowed_extensions`.
   - Add `--system` flag handling to `qdchrome-extension/scripts/install-native-host.sh` (if not already there).

5. **Image-pipeline integration** — same as Firefox; the rpm/deb that ships the policy + crx + native host should be part of the qdistro image build.

## Multi-browser story

Most qdistro users will install just one browser. But the system policy files for Firefox and Chromium are independent — shipping both is non-conflicting. A `qdistro-browser-bridge` meta-package can pull in:

- `qdistro-firefox-extension` (subpackage; ships policies.json + signed xpi + native host)
- `qdistro-chromium-extension` (subpackage; ships managed policy + crx + native host)

…and the user just gets the bridge in whichever browser they actually use.

## Verification (post-install)

```bash
# Policy is in effect
chromium --headless --dump-dom 'chrome://policy' 2>/dev/null | grep qdistro-chrome

# Extension is installed
ls ~/.config/chromium/Default/Extensions/<extension-id>/

# Native host wired
test -f /etc/chromium/native-messaging-hosts/qdistro.json
```

## See also

- [01-system-install-firefox.md](01-system-install-firefox.md) — symmetric flow for Firefox.
- Chromium's policy templates: https://chromeenterprise.google/policies/
- `../../qdchrome-extension/todo/` — where the actionable Chromium-side work belongs once promoted from todo to spec.
