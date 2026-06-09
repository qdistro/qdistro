# Browser integration

qdistro integrates with Firefox and Chromium-family browsers via a
WebExtension + native-messaging bridge. The bridge is the OS-level identity
boundary; the extension is the in-browser action surface. On top of this
transport, qdistro surfaces browser state in the desktop — history, tabs,
MPRIS media, downloads, share-to — analogous to KDE's Plasma Browser
Integration.

> **Status snapshot.** The bridge implements `qdistro.ping`, `recall.push`,
> and the Firefox `containers.*` relay path documented in
> [firefox-containers.md](firefox-containers.md). Most desktop integrations
> below remain specified/planned Phase-9 work.
>
> P0-1, P0-2, and P0-3 are landed. P0-4..P0-6 remain open deployment or
> policy decisions; see the defect index.

## Supported-browser matrix

qdistro supports **RPM-packaged Firefox and Chromium-family browsers on
Tumbleweed only**, by explicit design. Sandboxed browser distributions
(Snap Firefox, Flatpak Firefox/Chromium) intermediate native-messaging
launches through `xdg-desktop-portal` or `flatpak-spawn`, defeating the
bridge's `getppid()` parent-exe identity check.

The bridge fails closed with a `parent_not_allowed` error when its parent
isn't on the allowlist. The current default allowlist
(`ALLOWED_PARENT_EXES` in `qdistro_browser_bridge.py`) is:

| Browser | Path |
|---|---|
| Firefox | `/usr/lib64/firefox/firefox`, `/usr/lib/firefox/firefox` |
| Chromium | `/usr/bin/chromium`, `/usr/bin/chromium-browser` |
| Chrome | `/usr/bin/google-chrome`, `/usr/bin/google-chrome-stable` |
| Edge | `/usr/bin/microsoft-edge`, `/usr/bin/microsoft-edge-stable` |
| Brave | `/usr/bin/brave-browser`, `/usr/bin/brave` |
| Vivaldi | `/usr/bin/vivaldi`, `/usr/bin/vivaldi-stable` |

All entries are default-on; there is no opt-in mechanism in the current
build. If Brave/Vivaldi/Chrome/Edge should require explicit admin opt-in,
that is a code change in `_resolve_allowlist()` — not currently the case.
Snap/Flatpak users see "qdistro browser integration is not supported on
sandboxed browsers — install the RPM build."

> **Known defect (P0-2).** `_resolve_allowlist()` honors a
> `QDISTRO_BROWSER_BRIDGE_ALLOWLIST` environment variable that lets any
> process in the bridge's launch environment replace the allowlist
> entirely. The doc previously described the allowlist as the trust
> boundary; the env-var override is a hole.

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
on `exe in ALLOWED_PARENT_EXES`. This is the trust anchor: only a real
browser binary on the allowlist may invoke the bridge.

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

### `recall.push` — Phase 8 (implemented)

Text snapshot ingest from the extension. Writes to the per-day SQLite
database under `/var/lib/qdistro/recall/<user>/` (or the per-user dir
when no daemon is configured).

P0-3 fixed the former cross-silo write hazard: the destination user is derived
from the kernel-attested identity chain, not from an extension-supplied field.

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

- **Outbound-only ops** (`qdistro.ping`, `recall.push`, future
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

The admin panel has an "Install qdistro browser integration" action per
user-browser combination. **Production deployment depends on several
external pipelines that are not yet in place** — see "Deployment gaps"
below.

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

2. Install the extension. Distribution constraints:
 - **Firefox**: every XPI must be signed by Mozilla AMO, even when
 self-distributed (unlisted). Build pipeline depends on AMO's
 self-distribution sign API.
 - **Chromium**: CRX signed by qdistro's own key. Force-installed via
 the `ExtensionInstallForcelist` policy file pointing at an
 `update.xml` hosted by qdistro.
 - **Windows force-install** of a non-Web-Store extension additionally
 requires the host to be joined to an Active Directory or Azure AD
 domain, or enrolled in Chrome Enterprise Core. Not in scope for
 the Linux desktop target.

3. Register the user-browser pair in the daemon's policy.

The bridge binary lives at a fixed path `/usr/lib/qdistro/browser-bridge`
— one binary used by all users' browsers. Policy distinguishes callers
by parent identity + user UID. The installer's `--bridge-path` is
user-overridable, so a same-UID actor can re-point their manifest at any
binary; the bridge's parent-exe allowlist is the only barrier against
that, which is why the allowlist must not be env-overridable in
production builds (see P0-2).

### Deployment gaps (not yet resolved)

| Gap | Owner | Status |
|---|---|---|
| qdistro CRX signing key custody | release engineering | TBD |
| `update.xml` hosting endpoint | release engineering | installer defaults to `https://example.invalid/…` |
| AMO self-distribution build pipeline | release engineering | TBD |
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
- Ops that terminate at the bridge itself (today: `qdistro.ping` and
 `recall.push`) are logged by the bridge to the journal with tag
 `qdistro-browser-bridge`. `recall.push`'s database write is logged by
 the bridge, not by a downstream daemon — there is no
 `qdistro-recall` daemon involved in the default-impl write path.

The admin panel shows per-user-per-browser activity: operation,
timestamp, decision, identity chain.

## Known defects index

Historical fix-plan details were pruned from the public repo. Summary:

| ID | Defect | Severity | Status |
|---|---|---|---|
| P0-1 | `extension_id` read from stdio (untrusted) instead of argv (kernel-attested) | High | ✅ landed (commit `0c3a7a8`) |
| P0-2 | `QDISTRO_BROWSER_BRIDGE_ALLOWLIST` env-var bypasses the trust boundary | High | ✅ landed (commit `0c3a7a8`) |
| P0-3 | `recall.push` accepts extension-supplied `user` field — cross-silo write primitive | High | ✅ landed (commit `0c3a7a8`) |
| P0-4 | Browser allowlist (Brave/Vivaldi/Chrome/Edge) ships default-on with no opt-in flag | Medium | open — decision needed |
| P0-5 | Extension manifests don't declare permissions for any op past `ping` | Medium (Phase 9 blocker) | open — needs extension work |
| P0-6 | CRX signing key, `update.xml` hosting, AD/Azure-AD requirement on Windows unspecified | Medium (deployment blocker) | open — needs release-engineering |
