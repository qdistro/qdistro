# 01 — System-level injection (Firefox)

## Why

For qdistro to ship the browser bridge as part of the base image, the extension has to land in every Firefox profile **without the user being asked**. A user who's never opened `about:debugging` should still see the qdistro toolbar action the first time they launch Firefox.

## What Firefox supports

Two mechanisms exist; both run into the signature wall.

### a) Enterprise policy (preferred)

Drop a JSON file at one of:

- `/etc/firefox/policies/policies.json` (Linux distro path, recommended)
- `/usr/lib64/firefox/distribution/policies.json` (Mozilla-bundle path)
- `/etc/firefox/policies.json` (older Firefox builds may look here)

```json
{
  "policies": {
    "ExtensionSettings": {
      "qdistro-firefox@qdistro.local": {
        "installation_mode": "force_installed",
        "install_url": "file:///usr/share/qdistro/extensions/qdistro-firefox.xpi",
        "default_area": "navbar"
      }
    }
  }
}
```

`installation_mode` options:
- `force_installed` — installed; user cannot disable or remove.
- `normal_installed` — installed; user can disable but not remove.
- `allowed` — pre-allowlist for sideload.

### b) System-wide drop-in directory (legacy)

Place a signed xpi at:

```
/usr/lib/mozilla/extensions/{ec8030f7-c20a-464f-9b0e-13a3a9e97384}/qdistro-firefox@qdistro.local.xpi
/usr/share/mozilla/extensions/{ec8030f7-c20a-464f-9b0e-13a3a9e97384}/qdistro-firefox@qdistro.local.xpi
```

(The UUID is Firefox's stable application ID.) Firefox scans these on every launch and installs anything it finds.

## The signing wall

Release Firefox **refuses unsigned xpis regardless of install mechanism**. `xpinstall.signatures.required=false` is locked off for non-developer channels — not even policy can override it. So shipping a system-installed unsigned xpi only works on:

- Firefox ESR with `MOZ_REQUIRE_SIGNING=` empty at build time, OR
- Firefox Developer Edition / Nightly with the same build flag, OR
- A custom Firefox build qdistro maintains itself.

For a production qdistro install: **AMO-sign the xpi** via `web-ext sign --api-key=... --api-secret=...`. Self-distribution-only listings turn around in minutes; the signed xpi installs anywhere.

## Concrete deliverables

1. **`scripts/install-system-policy.sh`** — writes `/etc/firefox/policies/policies.json` (merging with any existing policy file via `jq`), points `install_url` at `/usr/share/qdistro/extensions/qdistro-firefox.xpi`. Idempotent. Refuses to overwrite a policy file with non-qdistro `ExtensionSettings` without `--force`.

2. **`scripts/install-native-host.sh --system` already exists** — keep it as the system-wide native-host installer.

3. **`scripts/build-extension.sh`** — **Scaffolded 2026-05-16.** `--sign` flag shells out to `web-ext sign --channel=unlisted` when `WEB_EXT_API_KEY` + `WEB_EXT_API_SECRET` are set and `web-ext` is on PATH; warns and skips otherwise. Emits `dist/firefox.xpi` (unsigned, always) and `dist/firefox-signed.xpi` (when signing succeeds). The actual sign has not been run yet — needs AMO credentials.

4. **An rpm/deb package** (or a `justfile` deploy target) that drops:
   ```
   /usr/share/qdistro/extensions/qdistro-firefox.xpi    # signed
   /etc/firefox/policies/policies.json                  # ExtensionSettings
   /usr/lib/mozilla/native-messaging-hosts/qdistro.json # system-wide native host
   ```

5. **Image-pipeline integration** — the qdistro baseweed / kiwi pipelines (see `project-image-pipelines` memory) should include the rpm so a fresh install boots with the extension already present.

## Non-deliverables

- Per-user install. The whole point is OS-level injection.
- Bypassing the AMO sign step. Custom Firefox builds are a separate qdistro track; don't conflate.

## Verification (post-install)

```bash
# Policy is in effect
firefox --headless about:policies 2>/dev/null  # surfaces "qdistro-firefox@qdistro.local: force_installed"

# Extension shows up
ls ~/.mozilla/firefox/*.default*/extensions/  # qdistro-firefox@qdistro.local.xpi present

# Native host wired
test -f /usr/lib/mozilla/native-messaging-hosts/qdistro.json
```

A passing scenario in `tests/integration/firefox-gui/` would assert all three after a fresh-VM boot. Blocked on the VM-GUI tooling gap (`project-vm-gui-tooling-gap` memory) until weston has XWayland + screencopy.

## See also

- [02-system-install-chromium.md](02-system-install-chromium.md) — symmetric flow for Chromium.
- Mozilla's policies templates: https://github.com/mozilla/policy-templates
