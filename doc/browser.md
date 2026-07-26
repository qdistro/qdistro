# Browser integration

qdistro integrates with Firefox and Chromium-family browsers via a
WebExtension + native-messaging bridge. The bridge is the OS-level identity
boundary; the extension is the in-browser action surface. On top of this
transport, qdistro surfaces browser state in the desktop — history, tabs,
MPRIS media, downloads, share-to — analogous to KDE's Plasma Browser
Integration.

> **Status snapshot.** The v1 bridge implements `qdistro.ping` and the
> Firefox `containers.*` relay path documented in
> [firefox-containers.md](firefox-containers.md). Most desktop integrations
> below remain specified/planned Phase-9 work.
>
> P0-1 through P0-5 are landed. P0-6 (extension distribution) is closed for
> v1 as documented manual load — see
> [browser-extension-install.md](browser-extension-install.md) and the defect
> index. Recall capture is cut from v1, so
> `recall.push` is not registered in the bridge dispatch table.

## Supported-browser matrix

qdistro supports **RPM-packaged Firefox and Chromium-family browsers on
Tumbleweed only**, by explicit design. Sandboxed browser distributions
(Snap Firefox, Flatpak Firefox/Chromium) intermediate native-messaging
launches through `xdg-desktop-portal` or `flatpak-spawn`, defeating the
bridge's `getppid()` parent-exe identity check.

The bridge fails closed with a `parent_not_allowed` error when its parent
isn't on the allowlist. The allowlist (`_resolve_allowlist()` in
`qdistro_browser_bridge.py`) has a **default-on baseline** and a set of
**admin-opt-in optional browsers** (P0-4):

| Browser | Path | Default |
|---|---|---|
| Firefox | `/usr/lib64/firefox/firefox`, `/usr/lib/firefox/firefox` | on |
| Chromium | `/usr/bin/chromium`, `/usr/bin/chromium-browser` | on |
| Chrome | `/usr/bin/google-chrome`, `/usr/bin/google-chrome-stable` | **opt-in** |
| Edge | `/usr/bin/microsoft-edge`, `/usr/bin/microsoft-edge-stable` | **opt-in** |
| Brave | `/usr/bin/brave-browser`, `/usr/bin/brave` | **opt-in** |
| Vivaldi | `/usr/bin/vivaldi`, `/usr/bin/vivaldi-stable` | **opt-in** |

Firefox and Chromium are trusted parents out of the box. Chrome, Edge,
Brave, and Vivaldi are **default-off**; the bridge rejects them as parents
until an admin opts each family in (P0-4 fix, mirroring the F4
firefox-containers opt-in). The opt-in surface is a **root-owned** config
file, `/etc/qdistro/browser-bridge-allowlist.conf`, with one browser key
per non-comment line:

```
# Optional browser families this machine trusts as bridge parents.
brave
chrome
```

Valid keys: `chrome`, `brave`, `vivaldi`, `edge`. The bridge runs as the
(unprivileged) browser-child uid, so the config is honored **only** when
it is a regular file (not a symlink) owned by root and not group/other
writable — otherwise it is ignored fail-closed and the baseline applies,
so the bridge's own uid can never widen its trust boundary (the same
lesson P0-2 applied to the rejected `QDISTRO_BROWSER_BRIDGE_ALLOWLIST`
env var). Snap/Flatpak users see "qdistro browser integration is not
supported on sandboxed browsers — install the RPM build."

> **Scope note (follow-up landed).** P0-4's opt-in lives in the *bridge
> entry gate* (`_resolve_allowlist()`), the authoritative barrier against a
> non-browser program exec'ing the bridge. The browser daemons' separate
> process-identity attestation and the pwd daemon's bridge gate now resolve
> the trusted parent set through the **same** shared module
> (`qdistro_browser_allowlist.resolve_parent_exes()`), so those
> defense-in-depth gates can no longer drift wider than the entry gate — an
> optional browser is rejected as a parent until an admin opts it in, at
> every gate. (Previously they carried an independent default-ON full matrix;
> not a leak, because the entry gate already rejected an un-opted-in parent
> before any forward, but the boundaries are now aligned by construction.)

> **P0-2 (closed).** `_resolve_allowlist()` once honored a
> `QDISTRO_BROWSER_BRIDGE_ALLOWLIST` environment variable that let any
> process in the bridge's launch environment replace the allowlist
> entirely. That override is gone: the legacy variable now hard-errors,
> and only `QDISTRO_BROWSER_BRIDGE_ALLOWLIST_TEST` under
> `QDISTRO_TEST_MODE=1` is accepted (test-only). The opt-in config above
> follows the same principle — the bridge's own unprivileged uid can
> never widen the trust boundary.

## Architecture

```
+---------------------+ +------------------+
| Browser | | qdistro daemon |
| (firefox/chromium) | | (pwd / other) |
| | | |
| +---------------+ | Native messaging | |
| | qdistro ext |<-+- stdin/stdout ---+----->| |
| +-------^-------+ | | | |
| | msg | v | |
| | | +-----+------+ |
| | | | qdistro- | D-Bus |
| | | | browser- |-----> daemon |
| | | | bridge | |
+----------+----------+ +------------+ |
 web page (untrusted) (per-browser process, |
 identity-pinned) |
```

- **Extension** — qdistro-authored WebExtension, installed in each browser
 that needs integration. Same codebase across Firefox and Chromium.
- **Native messaging** — the browser's built-in protocol for extension ↔
 native-process communication. JSON messages over stdin/stdout with a
 4-byte length prefix.
- **Bridge** — `qdistro-browser-bridge` (a small Python program), launched
 by the browser as a native-messaging host. One bridge process per
 `chrome.runtime.connectNative` connection, alive for the connection's
 lifetime.
- **Daemon** — the consumer service: `qdistro-pwd` for passwords,
 `qdistro-browser` for tabs/history, `qdistro-downloads`, etc.

## Identity chain

Browser runs as the user uid; the extension is inside the browser; web
content is inside renderers. None of these has a distinct OS identity to
trust. The **bridge process** is where OS-level identity attaches.

### 1. Parent process verification (implemented, Phase 8)

The bridge calls `getppid()`, reads `/proc/<ppid>/exe`, and gates dispatch
on membership in the effective allowlist (`_resolve_allowlist()` — the
Firefox+Chromium baseline plus any admin-opted-in optional browsers; see
the support matrix above). This is the trust anchor: only a real browser
binary on the allowlist may invoke the bridge.

`/proc/<ppid>/attr/current` (SELinux label) is also read but is **audit
information only** — there is no enforcement gate keyed on the label.
Per qdistro precedent (`qdistro-pwd`), SELinux labels are stored in
audit columns and pin records, not consulted at dispatch time.

> **Doc defect fixed.** Previous text said "Verifies the parent is an
> expected browser binary with an expected SELinux label." The SELinux
> half was false; the parent-exe half is real. Only the parent-exe check
> gates dispatch.

### 2. Bridge identity (partly implemented)

The bridge runs as the browser's UID at a fixed path
`/usr/lib/qdistro/browser-bridge` (or wherever the installer wrote the
native-messaging manifest's `path`). A future SELinux module would label
this binary `qdistro_browser_bridge_t`; **no such module currently
ships**. Bridge-side identity attaches via UID + parent-pinned exe path
only.

The installer's `--bridge-path` flag is user-overridable, so a same-UID
user can in principle point their per-user manifest at any binary. The
*daemon-side* trust gate (when one is added in Phase 9) must therefore
re-verify the bridge binary path against an admin-controlled list rather
than trusting the connecting process to be the canonical bridge.

### 3. Extension identity (Phase 8 implemented)

The browser passes the calling extension's origin to the bridge as a
command-line argument:

- **Chrome / Chromium-family**: `argv[1]` is the extension origin in
 the form `chrome-extension://<ID>/` (Linux/macOS). On Windows there is
 an additional second argument that is a Chrome window handle.
- **Firefox**: `argv[1]` is the full path to the host manifest;
 `argv[2]` is the extension ID (`browser_specific_settings.gecko.id`)
 since Firefox 55.

These argv values are set by the browser at exec time and are not
forgeable by extension JS. The bridge **should** parse them and treat
the result as the authoritative extension identity.

P0-1 fixed this path: the bridge parses argv at startup and treats the
browser-supplied extension identity as authoritative. Stdio-provided extension
identity is not trusted for policy.

#### Firefox extension artifacts (two canonical, by install mode)

There are **two** canonical Firefox extensions, deliberately distinct, each
authorized by its own `qdistro-browser-install --firefox-mode`:

| Mode | Source of truth | gecko id |
|------|-----------------|----------|
| `bundled` (default) | `browser_bridge/extension/` (MV2, shipped next to the installer) | `qdistro@qdistro.local` |
| `standalone` | the `qdfirefox-extension` repo (MV3, first-class containers) | `qdistro-firefox@qdistro.local` |

`qdistro_browser_install.py` reads the bundled id from
`browser_bridge/extension/manifest.firefox.json` and pins the standalone id as
a cross-repo contract (asserted by the unit suite). The policy-match example
below uses the **bundled** id because `bundled` is the default install mode —
swap it for `qdistro-firefox@qdistro.local` only when matching a standalone
install.

> **J11 caveat (open).** `install-browser-bridge-for-vm.sh` copies the
> *bundled* tree to `/usr/share/qdistro/browser-extension/`, and that tree has
> no `src/`, no `gate.js` and no origin allowlist; the closed-by-default gate
> lives only in the standalone repos, which no installer ships. So the
> extension the installer puts on disk is not the hardened one, and `bundled`
> is also the installer's DEFAULT `--firefox-mode`. Tracked as J11 in
> `todo/fable-release/10-reachability-audit-2026-07-26.md` and fixed
> separately; [browser-extension-install.md](browser-extension-install.md)
> steers users at the standalone artifacts in the meantime.

The Chromium extension (`qdchrome-extension`) does **not** build a Firefox
artifact: it formerly emitted an MV2 build under the same
`qdistro@qdistro.local` id as the bundled extension — a drift trap (two
distinct codebases, one id) — so that target was removed. Firefox ships from
one of the two sources above only.

### 4. Daemon policy (Phase 9 — not implemented)

Once Phase 9 daemons exist, the per-op polkit policy file
(`org.qdistro.browser.policy`, per-op actions in the style of
`org.qdistro.pwd.unlock`) gates dispatch. Match criteria will be drawn
from the identity stack:

```yaml
- match:
 service: pwd
 parent_exe: /usr/lib64/firefox/firefox
 extension_id: qdistro@qdistro.local  # from argv, not stdio
 caller_user: work-user
 action: allow
```

`bridge_selinux` / `parent_selinux` may appear in audit logs but **are
not** part of the dispatch-time match — see §Identity chain step 1.

Compromised web content can't reach the bridge at all. A compromised
extension can reach the bridge, but the bridge's parent-verification
ensures the extension is hosted in a real browser binary on the
allowlist. A malicious program *claiming* to be Firefox by exec'ing
under a fake name still fails the `/proc/<ppid>/exe` realpath check.

## Why a separate bridge

- Browsers sandbox extensions; extensions can't open arbitrary unix
 sockets.
- Native messaging is the designed extension-escape mechanism; it's vetted
 and rate-limited by the browser.
- The bridge is where OS-level identity attaches (parent-process exe
 check, UID, future SELinux label, cgroup) before anything touches a
 qdistro daemon.
- Extensions come and go; the bridge is a stable trust boundary.

## Operations

Protocol: JSON over native messaging (browser ↔ bridge), D-Bus upstream
(bridge ↔ daemon). Identity and policy evaluation sit entirely in the
bridge-daemon boundary.

### `qdistro.ping` — Phase 8 (implemented)

Round-trip echo with identity confirmation. The bridge replies with the
caller's resolved identity (parent exe, parent SELinux label,
extension_id as currently self-asserted, caller UID/username). Used by
the install flow to verify end-to-end connectivity.

### `recall.push` — post-v1 (disabled in v1)

Text snapshot ingest from the extension is deliberately disabled for v1.
The dormant Recall engine remains in the source tree, but the bridge does
not register `recall.push`, the shared op registry omits it, and the release
bootstrap profile does not install the Recall timer/service.

When Recall returns, the destination user must still be derived from the
kernel-attested identity chain, not from an extension-supplied field.

### `pwd.fill` / `pwd.save` — Phase 9a (not implemented)

Extension → bridge → `qdistro-pwd`. See
[password-manager](password-manager.md) for daemon-side handling. Will
require an `intent_token` (see below) for `pwd.save`.

### `page.extract` — Phase 9c (not implemented)

User selects content and chooses "Send to notebook" (extension
context-menu item). The bridge forwards to the cross-user broker, which
applies policy for the cross-silo transfer. Requires an `intent_token`.

Daemon-initiated page reads use `page.extract.request`; see
[browser-page-extract](browser-page-extract.md) for the wire shape, modes,
limits, examples, and security handling.

### `tabs.list`, `tabs.open`, `tabs.close` — Phase 9b (not implemented)

Bidirectional. A consumer daemon initiates → the daemon's D-Bus call
reaches the bridge's well-known name (e.g.
`org.qdistro.BrowserBridge.<ppid>` on the user session bus) → the bridge
forwards the request over stdio to the extension → the extension calls
`chrome.tabs.*` and replies → the bridge correlates by `request_id` and
returns to the daemon. Requires a persistent `connectNative` from the
extension service worker, plus the heartbeat below to keep an MV3 SW
alive across idle periods.

### `cookies.export` — Phase 9d (not implemented)

Sensitive. The extension UI has an explicit "Export session" action; the
user clicks to authorize. **Requires an `intent_token` (mandatory, not
"prompt").** Audit-logged at the destination. TTL on exported payload is
30 seconds at the destination by default (configurable per-policy).
Treated as a vault-grade item.

`cookies.export` accepts a `cookie_store_id` field (Firefox containers
only) to scope the export to a single contextual identity. See
[firefox-containers.md](firefox-containers.md).

### `containers.list` / `.create` / `.remove` — Firefox only (own-uid round-trip pinned 2026-05-16)

Firefox contextual identities (Multi-Account Containers). Bridge → ext
direction; the bridge enqueues the op down stdio and blocks on the
extension's reply, mirroring the `tabs.*` pattern. Same-uid daemons
reach these via the session-bus `RequestTabs` method; cross-uid daemons
go through `UserRelay.ForwardBrowserBridgeOp` on the system bus.

Wire shape, error codes, cross-uid routing model, and the impostor-name
gate are documented in [firefox-containers.md](firefox-containers.md).
Test coverage: `tests/unit/test_browser_bridge_phase9.py::TestContainersRequest`
(round-trip + Chromium-unavailable + timeout) and
`tests/unit/test_user_relay.py::TestForwardBrowserBridgeOp` (cross-uid
selector + audit).

### Heartbeat for persistent ports under Chromium MV3 — Phase 9b

Chromium MV3 service workers terminate after **30 seconds of inactivity**
and have a hard **5-minute cap on any single request**; as of Chrome 110,
any event or extension-API call resets the idle timer.
`chrome.runtime.connectNative` keeps the worker alive while messages are
actively flowing. Persistent-mode ops implement a heartbeat: the bridge
sends `qdistro.heartbeat` every 25 seconds; the extension responds with
`qdistro.heartbeat.ack`; the round-trip resets both sides' inactivity
timers. Firefox does not have this constraint, but the heartbeat is a
no-op on Firefox.

**Suspend-mid-request behavior:** if the SW suspends after the bridge
has dispatched a request but before a reply arrives, the bridge queues
the request and retries on the next heartbeat ack. After 3 retries
(~75 s wall-clock) the bridge returns a `request_timeout` to the daemon
and discards the `request_id`.

## Intent tokens — Phase 9d (not implemented)

Operations that a compromised extension could trigger silently
(`cookies.export`, `pwd.save`, `page.extract`) require an `intent_token`:
proof of recent user action in the extension's UI.

- Format: `{request_id, timestamp, operation, hmac}`. HMAC key is a
 per-session secret established at bridge startup via a
 `qdistro.handshake` op shared between the extension and the bridge.
- TTL: **5 seconds**. Scope: single `request_id` (not reusable).
- The bridge verifies the token before forwarding; the daemon may also
 re-verify if the policy file requests it.

**Threat scope.** Intent tokens defend against (a) replay of captured
messages and (b) web-page-triggered calls that lack a fresh user click.
They do **not** defend against a compromised extension (which can mint
its own tokens) or a compromised browser (which can bypass the bridge
entirely). This is intentional: the bridge raises the bar against silent
background exfiltration; defence against full extension compromise lives
in the browser's own sandbox and the WebExtension review process.

## Bridge lifetime

Native messaging is **always** a persistent process model: the bridge
starts when the extension calls `chrome.runtime.connectNative(name)` and
exits when the port closes. There is no per-message spawn. Within that
lifetime:

- **Outbound-only ops** (`qdistro.ping`, future
 `pwd.fill`, `pwd.save`, `page.extract`, `cookies.export`) — extension
 initiates, bridge responds, port may close immediately if the extension
 chooses one-shot `sendNativeMessage` or stay open if it uses
 `connectNative`.
- **Inbound-capable ops** (future `tabs.list`, MPRIS, share-to receive)
 — extension uses `connectNative` and holds the port open for the
 session. The bridge owns a well-known D-Bus name on the user session
 bus so daemons can push requests down to the extension via the
 correlated-`request_id` pattern in §`tabs.list` above.

## Per-user, per-browser installation

> **Planned vs shipped.** An admin-panel "Install qdistro browser
> integration" action per user-browser combination is *planned*; no such
> action exists in `admin_app/` today. The shipped v1 mechanism is the
> `qdistro-browser-install` CLI plus a manual extension load.

**v1 has no signed extension distribution channel: the extension is loaded
manually, per user, per browser.** The
operator-facing procedure, its friction, and its security implications are
[browser-extension-install.md](browser-extension-install.md); the pipelines
that would replace it are post-v1 (see "Deployment gaps" below).

1. Write the native-messaging manifest to the correct location:
 - **Firefox**: `~<user>/.mozilla/native-messaging-hosts/qdistro.json`
 - **Chromium**:
 `~<user>/.config/chromium/NativeMessagingHosts/qdistro.json`
 - **Chrome**:
 `~<user>/.config/google-chrome/NativeMessagingHosts/qdistro.json`
 - **Brave**, **Vivaldi**, **Edge for Linux**: analogous paths.

 Manifest fields are identical across browsers except for the
 allowed-extension keying:
 - Firefox uses `allowed_extensions` with raw extension IDs.
 - Chromium-family uses `allowed_origins` with
 `chrome-extension://<id>/`.

 These fields are *browser-side* gates: they tell the browser whether
 to launch this host for a given extension. The host does **not**
 receive them via stdio; it receives the calling extension's identity
 via argv (see §Identity chain step 3).

2. Install the extension. **In v1 this step is manual** — build the
 artifact from the manifest-pinned extension checkout and load it
 yourself (Firefox: `about:debugging` temporary add-on, removed on every
 browser restart; Chromium: developer-mode "Load unpacked", which
 persists and keeps a stable id because the extension's public key is
 pinned in `manifest.chromium.json`). Full procedure, friction, and
 threat discussion:
 [browser-extension-install.md](browser-extension-install.md).

 The signed-distribution constraints that shape the post-v1 channel:
 - **Firefox**: every XPI must be signed by Mozilla AMO, even when
 self-distributed (unlisted). Build pipeline depends on AMO's
 self-distribution sign API. An enterprise policy does not waive it.
 - **Chromium**: CRX signed by qdistro's own key. Force-installed via
 the `ExtensionInstallForcelist` policy file pointing at an
 `update.xml` hosted by qdistro.
 - **Windows force-install** of a non-Web-Store extension additionally
 requires the host to be joined to an Active Directory or Azure AD
 domain, or enrolled in Chrome Enterprise Core. Not in scope for
 the Linux desktop target.

3. *(Planned, not implemented.)* Register the user-browser pair in the
 daemon's policy. There is no command or code for this step today — the
 Phase-9 per-op policy file it belongs to is itself unimplemented
 (§"Daemon policy"). Nothing in the v1 install flow requires it.

The bridge binary lives at a fixed path `/usr/lib/qdistro/browser-bridge`
— one binary used by all users' browsers. Policy distinguishes callers
by parent identity + user UID. The installer's `--bridge-path` is
user-overridable, so a same-UID actor can re-point their manifest at any
binary; the bridge's parent-exe allowlist is the only barrier against
that, which is why the allowlist must not be env-overridable in
production builds (see P0-2).

### Deployment gaps (v1 disposition: manual load, signed channel post-v1)

R4 resolved these by **choosing manual load for v1** rather than building a
signing pipeline for a private-alpha audience (D12). They are not silently
open: each is a named post-v1 item, and the v1 substitute is documented in
[browser-extension-install.md](browser-extension-install.md).

| Gap | Owner | Status |
|---|---|---|
| qdistro CRX signing key custody | release engineering | **post-v1** — v1 loads unpacked; the pinned public key keeps the *unpacked* id stable, but a CRX keeps that id only if signed with the matching private key, which has no custody story yet |
| `update.xml` hosting endpoint | release engineering | **post-v1** — no endpoint; installer default is `https://example.invalid/…`, so `--install-policy` must not be used in v1 |
| AMO self-distribution build pipeline | release engineering | **post-v1** — `build-extension.sh --sign` exists but is inert without AMO credentials; v1 uses `about:debugging` temporary load (re-load on every restart) |
| Force-install policy scripts (`install-system-policy.sh`, both repos) | release engineering | **scaffolding** — they reference `/usr/share/qdistro/extensions/…`, a path nothing populates in v1 |
| Auto-update + revocation for a shipped extension | release engineering | **post-v1** — no update channel and no kill switch; a fix reaches users only by rebuild + re-load |
| Air-gapped fallback (no AMO / no update.xml) | architecture | not designed |

## Failure modes

- **Bridge missing or wrong version** — the extension reports the error
 to the user and **disables qdistro-mediated features**. It must **not**
 fall back to the browser's built-in autofill / built-in password
 manager / built-in download handling when the failed op was a
 qdistro-equivalent (e.g. `pwd.fill`). Falling back would defeat the
 silo isolation the bridge exists to enforce.
- **Daemon not running** — the bridge returns `daemon_unavailable`; the
 extension surfaces this clearly and does not retry silently.
- **Policy denies** — the extension shows "admin denied" clearly; no
 silent retry, no fallback to in-browser equivalents.

## Desktop integrations on top of the bridge

### Browser resources in workflows

Authenticated browser profiles are authority-bearing resources. A workflow may
attach a profile to complete OAuth or web login, route a temporary auth URL
into it, and return only the callback code or token to the requesting task.

Agent-assisted browser steps should prefer accessibility-tree roles, labels,
and refs over coordinates or screenshot-only actions. The workflow run records
the browser resource, intent token, callback result, and any generated
credential resource in lineage.

> **Status: Phase 9e — none of the desktop integrations below are
> implemented.** Each subsection describes the intended behavior and
> identifies the daemon it terminates at.

### History and bookmarks

The extension exposes `history.search`, `bookmarks.search`. The bridge
forwards to a `qdistro-browser` service (D-Bus name TBD —
`org.qdistro.Browser1`) which aggregates across configured browsers and
users. The admin launcher shows results tagged with the source user's
colour. Click → opens the URL in the originating user's browser (not
admin's own).

Requires extension permissions `history`, `bookmarks` — not currently
declared in either `manifest.firefox.json` or `manifest.chromium.json`.

### Tabs

`tabs.list` feeds:

- An admin-panel "all open tabs, across all users" view, grouped by user.
- Optional admin-taskbar mode (each tab is a task item — expensive; off
 by default).
- Per-user sessions see only their own tabs.

Requires extension permission `tabs` (not just `activeTab`, which only
exposes the currently active tab on user invocation).

### Media (MPRIS)

Browsers implement MPRIS2 for their own media. qdistro re-exposes them
cross-UID (which is bespoke; KDE Connect bridges MPRIS to *remote
machines* via TLS, not cross-UID on the same box):

- Each user's browser exposes MPRIS on its session bus.
- The bridge re-exposes them on `qbus-admin` under namespaced names:
 `org.mpris.MediaPlayer2.qdistro.work-user.firefox`.
- The admin media widget lists all registered players; clicking
 play/pause sends the MPRIS call through the bridge to the right
 user's browser.

**Spoof prevention.** The republished MPRIS name embeds the source UID,
and the bridge process holds the well-known name with its own UID
attestation (the bridge is parent-pinned to a real browser binary). A
process on the session bus that *claims* to be `work-user.firefox` but
runs under a different UID cannot acquire the namespaced name because
the broker rejects the registration based on `GetConnectionUnixUser`.

### Downloads

The extension watches the browser's download events and forwards progress
updates to `qdistro-downloads` (D-Bus name TBD —
`org.qdistro.Downloads1`). The admin notification area shows ongoing
downloads aggregated across all users; click → opens the file location
in the originating user's file manager.

Requires extension permission `downloads`.

### Screen-lock inhibit

A browser playing video / fullscreen presentation can request an inhibit
via the bridge. The admin compositor applies the inhibit if policy
allows for that user. Inhibits are auto-released when the bridge port
closes or the extension sends `screenlock.release`.

### Web notifications

The browser's Notifications API can reach OS notifications via the
bridge. Policy gates per origin per user. The admin panel has a "web
notification origins" manager.

**Content sanitization.** Notification bodies are attacker-controlled
strings (any origin the user has granted notification permission to can
inject arbitrary content). The compositor MUST treat the body as plain
text and never render markup or interpret URLs as clickable without
explicit policy opt-in per origin. Rate limit: 5 notifications per
origin per minute per user; excess notifications are dropped silently.

Requires extension permission `notifications`.

### Share to…

The browser's context menu gains qdistro destinations:

- Send URL to another user's browser ("Open in dev-user's firefox").
- Send page selection to another user's notebook.
- Send current page as an archive to admin's pastebin.
- Send URL to admin's read-later list.

Uses `page.extract` and the cross-user broker. The source user's policy
decides what destinations are offered. The destination user's policy
decides what content types are accepted; cross-user transfers route
through `qdistro_admin_cache.approvals` with
`action='browser.share_to'` and argv-scoped match keys, defaulting to
one-hour session grants (using the same cache-schema pattern as clipboard
transfers). Requires extension permission `contextMenus` plus an
`intent_token` per transfer.

## Cross-user defaults

Given the single-tenant model (admin and users are the same physical
person, data silos are for isolation, not privacy from self):

- **Each new feature defaults to own-user-only.** Admin must explicitly
 opt in per user-browser to surface that user's data in admin's
 aggregate views.
- Once admin has opted a user-browser into a feature, admin sees that
 feature's data for that user-browser across the admin panel. The
 "admin sees everything" framing applies *to opted-in features*, not
 to features by default.
- **Admin can enable privacy mode** per user to exclude a silo from
 aggregate views even after opt-in (e.g., finance-user excluded from
 history search).

Every cross-user surface is visually tagged with the source user's
colour so admin always knows whose data they are looking at.

> **Doc defect fixed.** Previous text contained two contradictory rules
> ("Admin sees everything by default" and "New integrations default to
> own-user-only"). The default is *own-user-only*; admin opt-in is
> explicit per feature per user-browser. This matches the rollout
> principle: reduce accidental leaks during feature rollout.

## Configuration per user-browser

The admin panel has a "Browser integration features" page with toggles
per feature per user-browser (each toggle defaults off):

```
work-user / firefox:
 [ ] History in launcher
 [ ] Bookmarks in launcher
 [ ] Tabs in admin taskbar
 [ ] Media controls
 [ ] Download notifications
 [ ] Screen-lock inhibit
 [ ] Web notifications (policy: <empty>)
 [ ] Share-to destinations: <empty>
```

Once admin enables a toggle, the relevant policy entries become editable
(e.g. web-notification origin allowlist, share-to destination list).

## Audit

Each operation is logged at the point where it terminates:

- Ops that reach a daemon (`pwd.fill`, `cookies.export`, `tabs.*`,
 `page.extract`, MPRIS calls, downloads, etc.) are logged by that
 daemon, following the precedent in `pwd/qdistro_pwd_audit.py`
 (columns: `caller_uid`, `caller_pid`, `caller_exe`, `caller_selinux`,
 `caller_cgroup`, op name, decision, timestamp).
- Ops that terminate at the bridge itself (today: `qdistro.ping`) are
 logged by the bridge to the journal with tag `qdistro-browser-bridge`.

The admin panel shows per-user-per-browser activity: operation,
timestamp, decision, identity chain.

## Known defects index

Historical fix-plan details were pruned from the public repo. Summary:

| ID | Defect | Severity | Status |
|---|---|---|---|
| P0-1 | `extension_id` read from stdio (untrusted) instead of argv (kernel-attested) | High | ✅ landed (commit `0c3a7a8`) |
| P0-2 | `QDISTRO_BROWSER_BRIDGE_ALLOWLIST` env-var bypasses the trust boundary | High | ✅ landed (commit `0c3a7a8`) |
| P0-3 | `recall.push` accepts extension-supplied `user` field — cross-silo write primitive | High | ✅ landed (commit `0c3a7a8`) |
| P0-4 | Browser allowlist (Brave/Vivaldi/Chrome/Edge) ships default-on with no opt-in flag | Medium | ✅ landed — optional browsers default-off, admin opt-in via root-owned `/etc/qdistro/browser-bridge-allowlist.conf` (see note) |
| P0-5 | Extension manifests don't declare permissions for any op past `ping` | Medium (Phase 9 blocker) | ✅ standalone manifests tightened to the minimal serviced-op set + closed-set test (bundled copy follow-up) — see note |
| P0-6 | CRX signing key, `update.xml` hosting, AD/Azure-AD requirement on Windows unspecified | Medium (deployment blocker) | ✅ closed for v1 by R4 — manual load documented in [browser-extension-install.md](browser-extension-install.md); signed distribution is post-v1 (see note) |

**P0-4/5/6 disposition (D5 op-set freeze + D2 Recall cut, 2026-06-12):**

- **P0-4 — bounded by the op-set freeze + F4, not eliminated.** Decision D5
  freezes the v1 bridge op set: no NEW ops are added for v1, and `recall.push`
  is removed (D2). It does **not** mean the bridge dispatches only `qdistro.ping`
  — the registered dispatch table still includes the existing Phase-8 /
  session ops (`qdistro.handshake`, `qdistro.heartbeat.ack`), the
  intent-token-gated handlers (`INTENT_TOKEN_REQUIRED_OPS`: `pwd.fill` /
  `pwd.fill_confirm` / `pwd.save`, `page.extract`, `cookies.export`,
  `clipboard.set`), and the identity-gated 9e relays (`mpris.publish`,
  `downloads.notify`, `notifications.show`, `screenlock.inhibit` / `.release`)
  which forward after `_identity_gate()` only. What changes for P0-4: the one
  cross-silo path, `containers.*` cross-uid forwarding, is now **default-deny /
  admin opt-in** via a broker rule (F4, `04-feature-completion.md`), and the
  op surface is frozen so the allowlist's breadth cannot grow new capability.
  The sensitive handlers remain gated by their existing intent-token / identity
  checks regardless of which allowlisted browser hosts the bridge.
  **Update (landed):** the parent-browser allowlist opt-in is no longer
  deferred — Chrome/Edge/Brave/Vivaldi are now default-OFF and require an
  admin to opt each family in via the root-owned
  `/etc/qdistro/browser-bridge-allowlist.conf` (`_resolve_allowlist()`),
  mirroring the F4 firefox-containers opt-in. Firefox + Chromium remain the
  default-on baseline. The config is honored only when root-owned and not
  group/other-writable, so the bridge's own unprivileged uid cannot widen the
  trust boundary (the P0-2 lesson). See the "Browser support matrix" section.
- **P0-5 — bounded by the freeze; tightening landed.** With the op set frozen,
  the extension's declared-permission surface cannot grow in v1; tightening the
  manifests to declare exactly the shipped ops they use remains worthwhile but
  is no longer a moving target. It becomes live work again only when the op set
  is unfrozen post-v1. **Update (landed):** the standalone extension manifests
  (`qdchrome-extension`, `qdfirefox-extension`) now declare the minimal set the
  serviced ops use — `activeTab` (redundant with the `<all_urls>` host grant +
  `tabs`) and `webNavigation` (no navigation listener; page.extract injects via
  `scripting.executeScript`, pwd-fill is a static content script) were dropped,
  and a `tests/manifest.test.js` closed-set/absent/host-pin pins the minimal
  surface against silent regrowth. The bundled `browser_bridge/extension` copy
  is now aligned too: its manifests also drop `activeTab` and `webNavigation`,
  and `browser_bridge/extension/tests/manifest.permissions.test.js` pins the
  absent permissions, host grants, and empty optional-permission buckets.
- **P0-6 — closed for v1 as "manual load", not as "solved".** CRX signing key
  custody, `update.xml` hosting, and the Windows AD/Azure-AD enrollment
  requirement are distribution concerns, not bridge-security defects. R4
  (`todo/fable-release/03-release-engineering.md`) explicitly permits
  documenting manual load for v1, and D12 (private alpha) makes an AMO/CRX
  signing pipeline disproportionate. The v1 answer is therefore
  [browser-extension-install.md](browser-extension-install.md): build the
  artifact from the manifest-pinned extension source and hand-load it
  (Firefox temporary add-on — gone on every restart; Chromium unpacked —
  persistent, stable id). What that costs — no publisher signature at load
  time, no auto-update, no revocation — is stated there rather than papered
  over. The signing/hosting pipeline moves to the post-v1 list.
  Two install-path defects found while closing this and fixed here: the
  installer's default Chromium extension id was the placeholder
  `qdistroqdistroqdistroqdistroaaaaaaaa` (not a valid Chromium id at all, so
  every Chromium-family native-messaging manifest authorized an extension that
  cannot exist), and the extension repos were absent from the bootstrap fetch
  set and the release manifest's repo set, so no pinned extension source
  reached an install.
