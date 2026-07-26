# Installing the browser extensions (v1)

How a v1 user gets the qdistro browser integration working, what it costs
them, and what it does not protect against. The architecture — bridge,
identity chain, op set — is [browser.md](browser.md); this page is only the
distribution and install story.

> **v1 ships no signed extension distribution channel.** There is no
> AMO-signed XPI, no CRX signing key, no `update.xml`, no force-install
> policy that works, and no auto-update. Both extensions are **loaded
> manually, per user, per browser, from a locally built artifact**. That is
> a deliberate v1 scope decision (D1 bootstrap-only install, D12 private
> alpha) recorded in `todo/fable-release/03-release-engineering.md` R4,
> which permits "document manual-load for v1". Signed distribution is
> **post-v1** — see [Post-v1](#post-v1-what-is-deliberately-not-here).

## What the installer does — and does not — do

The bootstrap's `browser-bridge` chain step
(`scripts/install/install-browser-bridge-for-vm.sh`) installs the **host
side**. The paths that matter for this page (the step also installs the
shared allowlist module, the four Phase-9e daemon programs, optionally the
`qdbrowser` python package, and `systemctl --global enable`s the four
`qdistro-{downloads,mpris,notifications,compositor}` user services — this
table is a summary, not an exhaustive install record):

| Path | What |
|---|---|
| `/usr/lib/qdistro/browser-bridge` | the native-messaging host (an exec stub) |
| `/usr/libexec/qdistro/qdistro_browser_bridge.py` | the bridge itself |
| `/usr/libexec/qdistro/qdistro_browser_install.py` | the manifest writer |
| `/usr/local/bin/qdistro-browser-install` | CLI front for the above |
| `/usr/share/qdistro/browser-extension/` | source of the *bundled* extension — **not the extension this page installs**, see the warning below |

It does **not** install any extension into any browser profile, and it
writes no packed artifact: there is no `/usr/share/qdistro/extensions/`
directory on a v1 install. Loading the extension is a manual user step in
each browser.

### Where the extension source comes from

The bootstrap clones `qdchrome-extension` and `qdfirefox-extension` into the
source root as **optional, source-only** repos, and in a hardened profile
`verify_repo_pin` checks each clone out at the commit the signed release
manifest pins (see [release-signing.md](release-signing.md)). Nothing is
built or root-installed from those trees.

Be precise about what that buys you, because the next section depends on it:

- The pin is verified **at fetch time**. `verify_repo_pin` does reject a
  dirty or untracked checkout at that point, and the hardened bootstrap keeps
  the tree root-owned afterwards (`assert_trusted_tree`). What it does **not**
  do is re-run when you later build — so the recipe below re-checks HEAD and
  cleanliness itself, immediately before exporting.
- Because these repos are **optional**, a failed clone warns and the install
  continues — you then simply have no extension source and no browser
  integration. Under `--skip-sources` only *present* checkouts are pin-
  verified, so an absent extension repo is not an install failure either.
- When a clone *does* succeed in a hardened profile, a manifest with no pin
  for that repo is fatal. `gen-source-manifest.sh` emits both repos, so a
  generated release manifest is complete by construction; a hand-written one
  must include them (or the install must run `--profile=dev`).

### Build from a clean export, as your own user

The bootstrap runs as root and the source root it creates is root-owned,
while both build scripts write `dist/` **inside the source tree** — so do not
build in place, and do not build as root. Verify the manifest, verify the
checkout against it, export that exact commit into a fresh directory you own,
and build there. The script below is fail-closed: every check aborts.

```bash
#!/bin/bash
set -euo pipefail

REPO=qdchrome-extension          # or qdfirefox-extension
SRC=/opt/qdistro-src/$REPO
QD=/opt/qdistro-src/qdistro

# The PUBLISHED manifest + detached signature + release keyring you were
# given (the bootstrap can be pointed elsewhere via QDISTRO_SOURCE_MANIFEST /
# --manifest-sig / --release-keyring, so name the real files here rather than
# assuming the in-tree default):
MANIFEST=$QD/scripts/install/source-manifest.txt
SIG=$MANIFEST.sig
KEYRING=$HOME/qdistro-release-keyring.gpg
SIGNER=<40-hex-release-key-fingerprint>

# 1. Signature + format of the manifest itself (gpgv + authoritative signer).
"$QD/scripts/install/verify-source-manifest.sh" \
    "$MANIFEST" "$SIG" "$KEYRING" "$SIGNER"

# 2. Exactly one pin for this repo, and the checkout is AT it and CLEAN.
#    $SRC is ROOT-owned (the bootstrap cloned it), and git refuses to touch a
#    repository owned by someone else — hence `-c safe.directory`, which is
#    honoured in the command scope. `--no-optional-locks` keeps `status` from
#    trying to refresh an index it cannot write.
git_src() { git --no-optional-locks -c safe.directory="$SRC" -C "$SRC" "$@"; }

PIN=$(awk -v r="$REPO" '/^[[:space:]]*#/{next} $1==r{print $2}' "$MANIFEST")
[ "$(printf '%s\n' "$PIN" | grep -c .)" -eq 1 ] \
    || { echo "no unique pin for $REPO in $MANIFEST" >&2; exit 1; }
printf '%s' "$PIN" | grep -qE '^[0-9a-f]{40}$' \
    || { echo "pin for $REPO is not a 40-hex sha" >&2; exit 1; }
[ "$(git_src rev-parse HEAD)" = "$PIN" ] \
    || { echo "$SRC HEAD is not the pinned commit" >&2; exit 1; }
[ -z "$(git_src status --porcelain)" ] \
    || { echo "$SRC has modified or untracked files" >&2; exit 1; }

# 3. Export that commit into a FRESH directory you own, and build there.
#    (A reused directory is not a clean export: `git archive | tar -x`
#    overwrites archived paths but leaves stale extra files behind, and both
#    build scripts glob src/modules/*.js, src/content/*.js and icons/*.)
OUT=$(mktemp -d "$HOME/qdistro-ext.XXXXXX")
git_src archive "$PIN" | tar -x -C "$OUT"
cd "$OUT" && bash scripts/build-extension.sh
```

The browser performs no qdistro artifact check when you load the result, so
this script is the whole integrity story for the artifact — see
[the security section](#what-manual-load-costs-you-security-wise). Until the
v1 release key is published (`doc/release-signing.md`), step 1 has nothing to
verify against, and what you are trusting is your own copy of the source.

## The three extension artifacts

| Artifact | Source | Browser | Extension id |
|---|---|---|---|
| bundled | `qdistro/browser_bridge/extension/` | Firefox (MV2) | `qdistro@qdistro.local` |
| standalone Firefox | `qdfirefox-extension` | Firefox (MV3) | `qdistro-firefox@qdistro.local` |
| Chromium | `qdchrome-extension` | Chromium family (MV3) | `ammgnkddbnjdhikklpljgiclldedgncf` |

The two Firefox artifacts are distinct on purpose (see browser.md,
"Firefox extension artifacts"); the standalone one is the maintained
first-class build and the one this page uses. The bundled tree's Chromium
manifest carries no `key` field, so it has no stable Chromium id — for
Chromium-family browsers use `qdchrome-extension`.

> **Do not load the copy the installer leaves in
> `/usr/share/qdistro/browser-extension/`.** That directory is a copy of the
> *bundled* tree, and the bundled tree is an older, flat extension: no `src/`
> directory, no `gate.js`, and therefore **no origin allowlist at all** — the
> closed-by-default gate that J11 added lives in `qdchrome-extension`/
> `qdfirefox-extension`, which no installer ships. So the extension the
> installer puts on disk is not the extension this page tells you to build,
> and it is the weaker of the two. Build from the manifest-pinned standalone
> repos as described below.
>
> That mismatch is tracked as **J11** in
> `todo/fable-release/10-reachability-audit-2026-07-26.md` and is being fixed
> separately (deciding which extension actually ships, and shipping it). Until
> that lands, treat `/usr/share/qdistro/browser-extension/` as dead weight on
> disk, not as an install source.

## Firefox

```bash
cd <source-root>/qdfirefox-extension
bash scripts/build-extension.sh    # no Node/npm: bash + coreutils (zip only for the .xpi)
# -> dist/firefox/       unpacked tree
# -> dist/firefox.xpi    packed, UNSIGNED

qdistro-browser-install --browsers firefox --firefox-mode standalone
# -> ~/.mozilla/native-messaging-hosts/qdistro.json
#    ("allowed_extensions": ["qdistro-firefox@qdistro.local"])
```

`--firefox-mode standalone` is **not** optional here: the installer's default
mode is `bundled`, which writes `allowed_extensions: ["qdistro@qdistro.local"]`
— the id of the gate-less bundled tree, not of the extension you just built.

Then, in Firefox: `about:debugging` → **This Firefox** → **Load Temporary
Add-on…** → select `dist/firefox/manifest.json`. Verify with the toolbar
button → **Ping**: the popup shows `connected` and a `qdistro.ping` reply
carrying your resolved identity.

**The restart reality — read this before choosing Firefox.** A temporary
add-on is removed when Firefox exits. **Every Firefox restart requires
re-loading the extension through `about:debugging`.** There is no supported
way around that in v1:

- Standard (release/beta) Firefox — what Tumbleweed ships — requires every
  XPI to be signed by Mozilla, including self-distributed unlisted ones.
  `dist/firefox.xpi` is unsigned.
- `xpinstall.signatures.required=false` is honoured only by builds that
  permit it (Developer Edition, Nightly, unbranded, ESR). qdistro tests
  neither; the supported-browser matrix in [browser.md](browser.md) does not
  distinguish channels, so treat using one as your own risk decision.
- `scripts/install-system-policy.sh` in `qdfirefox-extension` writes an
  enterprise `ExtensionSettings` / `force_installed` policy pointing at
  `file:///usr/share/qdistro/extensions/qdistro-firefox.xpi`. In v1 that
  path is **never populated**, and an enterprise policy does not exempt an
  add-on from signing on release Firefox. The script is scaffolding for the
  post-v1 signed channel, **not** a v1 install path.

**Temporary loading also bypasses the normal install UX.** `about:debugging`
is a developer mechanism: it does not show the installation-time permission
prompts a normally-installed add-on would. Read the manifest's `permissions`
and `host_permissions` before you load it — this extension asks for
`<all_urls>`, `cookies`, `tabs`, `scripting` and native messaging. Unloading
or restarting removes the add-on, but any state it wrote (extension storage,
options) can persist in the profile.

If per-restart re-loading is unacceptable, the honest answer for v1 is: use
a Chromium-family browser for the bridge, or wait for the signed channel.

## Chromium family

```bash
cd <source-root>/qdchrome-extension
bash scripts/build-extension.sh    # no Node/npm; zip only for dist/chromium.zip

qdistro-browser-install --browsers chromium
# -> ~/.config/chromium/NativeMessagingHosts/qdistro.json
#    ("allowed_origins": ["chrome-extension://ammgnkddbnjdhikklpljgiclldedgncf/"])
```

Then, in the browser: `chrome://extensions` → enable **Developer mode** →
**Load unpacked** → select `dist/chromium/`. Verify with the toolbar button
→ **Ping**.

- **It survives restarts.** Unlike Firefox's temporary add-on, an unpacked
  Chromium extension stays loaded. Developer mode stays enabled for that
  profile and the extension stays a developer (unpacked) extension; whether
  the browser nags about that at startup is product- and platform-dependent
  (the well-documented "Disable developer mode extensions" bubble is a
  Windows/macOS behaviour, so do not count on it as a Linux signal).
- **The id is stable for the unpacked load.** `manifest.chromium.json` pins
  the extension's public key in its `"key"` field, so Chromium derives the
  same id (`ammgnkddbnjdhikklpljgiclldedgncf`) every time you load the tree
  — which is what lets the native-messaging manifest above be written
  *before* the extension is loaded and still match. A future packed CRX
  keeps that id only if it is signed with the **private** key matching this
  public one; that private key is developer-local and gitignored today
  (`qdchrome-extension/keys/README.md`), which is exactly the custody
  problem the post-v1 signed channel has to solve.
- **Chrome / Edge / Brave / Vivaldi additionally need an admin opt-in.**
  Those families are rejected as bridge parents until an admin lists them in
  the root-owned `/etc/qdistro/browser-bridge-allowlist.conf` (browser.md,
  supported-browser matrix). Chromium needs no opt-in.
- **Do not use `--install-policy` in v1.** It writes an
  `ExtensionInstallForcelist` entry whose update URL defaults to
  `https://example.invalid/qdistro-update.xml` — there is no hosted
  `update.xml` in v1, so the policy cannot install anything.

## What manual load costs you, security-wise

Be clear-eyed about what is and is not protected here.

- **No publisher signature on what you load.** Nothing verifies the artifact
  at load time — not the browser, not the bridge. The only integrity
  available is the one you perform yourself, at build time: verify the signed
  release manifest, check the extension checkout is at its pinned commit and
  clean, export that commit, and build from the export (the commands are in
  "Build from a clean export" above). The bootstrap's fetch-time pin does
  **not** carry forward to the tree you build later, and no artifact hash is
  produced or checked. If you obtain the extension any other way, you have no
  integrity story at all.
- **No auto-update and no revocation.** A security fix to an extension
  reaches you only when you rebuild and re-load it by hand. If a build turns
  out to be bad there is no channel to push a replacement and no kill
  switch. This is the single biggest reason the signed channel is a
  post-v1 blocker rather than a nice-to-have.
- **The bridge authenticates the browser, not the extension.** The bridge's
  trust anchor is the parent-exe check (`/proc/<ppid>/exe` against the
  allowlist). The *extension* identity comes from argv supplied by that
  browser, and which extensions may reach the host is decided browser-side
  by `allowed_extensions` / `allowed_origins`. So the bridge's guarantee is
  "a real allowlisted browser launched me on behalf of extension X" — it is
  not a statement that X's code is the code qdistro shipped.
- **Extension ids do not authenticate manually loaded code.** Chromium's
  manifest `"key"` is a *public* key: any unpacked tree that copies it
  receives the same authorized id, with no need to touch the native-host
  manifest. Firefox's gecko id is likewise a self-asserted manifest string.
  `allowed_origins` / `allowed_extensions` restrict which browser-reported
  id may launch the native host; neither proves the loaded JavaScript is
  qdistro's. Treat both ids as routing identifiers, not as substitution
  detection. (And note the attacker who can rewrite your `dist/` runs as
  your uid, so they can equally rewrite
  `~/.config/.../NativeMessagingHosts/qdistro.json` or re-point
  `--bridge-path` — see browser.md, identity chain step 2.)
- **The extension is highly privileged.** Its declared set is
  `nativeMessaging`, `tabs`, `cookies`, `downloads`, `notifications`,
  `contextMenus`, `scripting`, `storage` plus `<all_urls>` (Firefox also
  `contextualIdentities`). That is the minimal set for the ops the extension
  *code* implements — which is broader than the effective v1 bridge surface
  (`qdistro.ping` + Firefox `containers.*` under D5); see browser.md's
  P0-4/5/6 disposition note for what the dispatch table still registers. In
  practice a malicious artifact can read the cookies and inject into the
  pages covered by those granted host permissions in that browser profile
  (browser-internal and otherwise restricted pages excepted) — i.e. a
  browsing-data compromise of that silo. This is why the build-from-verified-
  export path above is the only supported one.
- **Developer mode stays on.** The Chromium flow leaves developer mode
  enabled for the profile, which keeps "Load unpacked" available to anything
  that can drive that browser profile locally.

The bridge-side mitigations that *do* apply are unchanged by manual load:
the parent-exe allowlist with root-owned opt-in for the optional browser
families, the intent tokens on the sensitive handlers (HMAC-keyed off the
`qdistro.handshake` session secret), and the default-deny broker rule on
cross-uid `containers.*` forwarding. None of them authenticate the extension
code; they bound what a compromised one can reach.

## Post-v1: what is deliberately not here

| Gap | Why it is not in v1 |
|---|---|
| AMO-signed (or self-distributed unlisted) XPI | needs an AMO account + credential custody; `scripts/build-extension.sh --sign` exists but is inert without `WEB_EXT_API_KEY`/`WEB_EXT_API_SECRET` |
| CRX signing key custody | `keys/qdistro.pem` is developer-local and gitignored; production signing needs custody, backup, and rotation decisions (D8-style) |
| `update.xml` hosting endpoint | no hosted endpoint; the installer default is `https://example.invalid/…` |
| `ExtensionInstallForcelist` / `ExtensionSettings` force-install | the `install-system-policy.sh` scripts in both extension repos are scaffolding for the signed channel; the artifact paths they reference are never populated in v1 |
| Auto-update + revocation | follows from the two above |
| Air-gapped fallback (no AMO, no update.xml) | not designed |
| Windows AD / Azure-AD enrollment for non-Web-Store force-install | out of scope — the target is the Linux desktop |

## Release CI note

The two extension repos carry a byte-identical
`tests/fixtures/golden-frames.js` — the bridge wire-protocol contract — with
no workspace linking them. `tests/golden-frames-drift.test.js` enforces
identity but, when the sibling repo is absent, warns and exits green. Release CI
must check both repos out side-by-side (or point `$QDISTRO_SIBLING_GOLDEN`
at the sibling fixture) **and** set `$QDISTRO_REQUIRE_SIBLING=1`, which
makes an absent sibling fatal. qci's host gate does both
(`ci/lib/gates/host.sh`), and `tests/integration/qci/extension-drift-guard.bats`
keeps that wiring from being removed.
