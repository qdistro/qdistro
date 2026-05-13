# Browser integration

qdistro integrates with Firefox and Chromium-family browsers via a
WebExtension + native-messaging bridge. The bridge is the OS-level identity
boundary; the extension is the in-browser action surface. On top of this
transport, qdistro surfaces browser state in the desktop — history, tabs,
MPRIS media, downloads, share-to — analogous to KDE's Plasma Browser
Integration.

## Supported-browser matrix

qdistro supports **RPM-packaged Firefox and Chromium on Tumbleweed only**,
by explicit design. Sandboxed browser distributions (Snap Firefox, Flatpak
Firefox/Chromium) intermediate native-messaging launches through
`xdg-desktop-portal` or `flatpak-spawn`, defeating the bridge's
`getppid()` parent-exe identity check.

The bridge fails closed with a clear error when its parent isn't on the
allowlist (`/usr/lib64/firefox/firefox`, `/usr/bin/chromium`, plus Brave
and Vivaldi if explicitly configured). Snap/Flatpak users see "qdistro
browser integration is not supported on sandboxed browsers — install the
RPM build."

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
 by the browser as a native-messaging host. Each browser spawn has its
 own bridge process.
- **Daemon** — the consumer service: `qdistro-pwd` for passwords,
 `qdistro-browser` for tabs/history, `qdistro-downloads`, etc.

## Identity chain

Browser runs as the user uid; the extension is inside the browser; web
content is inside renderers. None of these has a distinct OS identity to
trust. The **bridge process** is where OS-level identity attaches.

1. **Browser launches the bridge** as a child process. The bridge calls
 `getppid()`, reads `/proc/<ppid>/exe` and `/proc/<ppid>/attr/current`.
 Verifies the parent is an expected browser binary with an expected
 SELinux label (e.g., `firefox_exec_t`).
2. **Bridge has its own identity** — fixed path
 `/usr/lib/qdistro/browser-bridge`, SELinux type
 `qdistro_browser_bridge_t`. The target daemon verifies via the layered
 identity stack.
3. **Extension identity** — the browser's native-messaging config tells
 the bridge which extension manifest invoked it (extension ID). The
 bridge forwards this up.
4. **Daemon policy matches the full chain:**

```yaml
- match:
 service: pwd
 bridge_selinux: user_t:qdistro_browser_bridge_t
 parent_exe: /usr/bin/firefox
 parent_selinux: user_t:firefox_exec_t
 extension_id: qdistro@qdistro.local
 caller_user: work-user
 action: allow
```

Compromised web content can't reach the bridge at all. A compromised
extension can reach the bridge, but the bridge's parent-verification
ensures the extension is hosted in the real browser binary. A malicious
program *claiming* to be firefox but with a different SELinux label fails.

## Why a separate bridge

- Browsers sandbox extensions; extensions can't open arbitrary unix
 sockets.
- Native messaging is the designed extension-escape mechanism; it's vetted
 and rate-limited by the browser.
- The bridge is where OS-level identity attaches (SELinux label,
 parent-process check, cgroup) before anything touches a qdistro daemon.
- Extensions come and go; the bridge is a stable trust boundary.

## Operations

Protocol: JSON over native messaging (browser ↔ bridge), D-Bus upstream
(bridge ↔ daemon). All payload fields are informational; identity and
policy evaluation sit entirely in the bridge-daemon boundary.

### `pwd.fill` / `pwd.save`

Extension → bridge → `qdistro-pwd`. See [password-manager](password-manager.md)
for daemon-side handling.

### `page.extract`

User selects content and chooses "Send to notebook" (extension context-menu
item). The bridge forwards to the cross-user broker, which applies policy
for the cross-silo transfer.

### `tabs.list`, `tabs.open`, `tabs.close`

Bidirectional. A consumer app asks the daemon; the daemon forwards to the
bridge; the bridge asks the extension; the extension replies. Requires a
persistent bridge process so the extension has a stable message channel.

### `cookies.export`

Sensitive. The extension UI has an explicit "Export session" action; the
user clicks to authorize. Default policy: prompt. Treated as a
vault-grade item — audit-logged, possibly TTL-limited at the destination.

### Heartbeat for persistent ports under Chromium MV3

Chromium MV3 service workers terminate after 30 seconds of inactivity and
have a hard 5-minute execution cap. `chrome.runtime.connectNative` keeps
the worker alive *only while messages are actively flowing*. Persistent-
mode services implement a heartbeat: the bridge sends a
`qdistro.heartbeat` message every 25 seconds; the extension responds with
`qdistro.heartbeat.ack`; the round-trip resets both sides' inactivity
timers. Firefox does not have this constraint, but the heartbeat is
harmless on Firefox.

## Intent tokens

Operations that a compromised extension could trigger silently
(cookies.export, pwd.save, page.extract) require an `intent_token` — proof
of recent user action in the extension's UI.

- The extension issues a token on button click / keyboard shortcut, scoped
 to a request-id and short TTL.
- The daemon verifies the token before honouring the operation.

This is the in-browser analog of compositor attestation. Neither is a
perfect defence against a fully malicious extension (it could fake its own
clicks), but both raise the bar substantially and make background silent
exfiltration detectable.

## Bridge lifetime

- **Per-message spawn** — the browser launches the bridge per request;
 the bridge exits when done. Simplest. Used for one-shot ops
 (`pwd.fill`, `pwd.save`).
- **Persistent connection** — the browser keeps the bridge process alive
 for the tab's / session's lifetime. Needed for inbound requests
 (`tabs.list`) where the daemon initiates.

Admin policy picks per service.

## Per-user, per-browser installation

The admin panel has an "Install qdistro browser integration" action per
user-browser combination:

1. Write the native-messaging manifest to the correct location:
 - **Firefox**: `~<user>/.mozilla/native-messaging-hosts/qdistro.json`
 - **Chromium**:
 `~<user>/.config/chromium/NativeMessagingHosts/qdistro.json`
 - **Chrome**:
 `~<user>/.config/google-chrome/NativeMessagingHosts/qdistro.json`
 - **Brave**, **Vivaldi**, **Edge for Linux**: analogous paths.

 Manifest fields are identical across browsers except for the
 allowed-extension keying (Firefox uses `allowed_extensions` with raw
 extension IDs; Chromium-family uses `allowed_origins` with
 `chrome-extension://<id>/`). The install tool generates both shapes
 from one source-of-truth.

2. Install the extension. Distribution constraints:
 - **Firefox**: every XPI must be signed by Mozilla AMO, even when
 self-distributed (unlisted). Build pipeline depends on AMO's sign
 API.
 - **Chromium**: CRX signed by qdistro's own key. Force-installed via
 `ExtensionInstallForcelist` policy file.

3. Register the user-browser pair in the daemon's policy.

The bridge binary lives at a fixed path `/usr/lib/qdistro/browser-bridge`
— one binary used by all users' browsers. Policy distinguishes callers
by parent identity + user uid.

## Failure modes

- **Bridge missing or wrong version** — the extension reports the error
 and falls back to the browser's built-in behaviour.
- **Daemon not running** — the bridge reports up; the extension degrades
 gracefully.
- **Policy denies** — the extension shows "admin denied" clearly; no
 silent retry.

## Desktop integrations on top of the bridge

The bridge transports browser data into qdistro's DE surfaces.

### History and bookmarks

The extension exposes `history.search`, `bookmarks.search`. The bridge
forwards to a `qdistro-browser` service which aggregates across configured
browsers and users. The admin launcher shows results tagged with the source
user's colour. Click → opens the URL in the originating user's browser
(not admin's own).

### Tabs

`tabs.list` feeds:

- An admin-panel "all open tabs, across all users" view, grouped by user.
- Optional admin-taskbar mode (each tab is a task item — expensive; off
 by default).
- Per-user sessions see only their own tabs.

### Media (MPRIS)

Browsers implement MPRIS2 for their own media. qdistro bridges them up
cross-uid (which is bespoke; KDE Connect bridges MPRIS to *remote
machines* via TLS, not cross-uid on the same box):

- Each user's browser exposes MPRIS on its session bus.
- The bridge re-exposes them on `qbus-admin` under namespaced names:
 `org.mpris.MediaPlayer2.qdistro.work-user.firefox`.
- The admin media widget lists all registered players; clicking play/pause
 sends the MPRIS call through the bridge to the right user's browser.

### Downloads

The extension watches the browser's download events and forwards progress
updates to `qdistro-downloads`. The admin notification area shows ongoing
downloads aggregated across all users; click → opens the file location in
the originating user's file manager.

### Screen-lock inhibit

A browser playing video / fullscreen presentation can request an inhibit
via the bridge. The admin compositor applies the inhibit if policy allows
for that user.

### Web notifications

The browser's Notifications API can reach OS notifications via the bridge.
Policy gates per origin per user. The admin panel has a "web notification
origins" manager.

### Share to…

The browser's context menu gains qdistro destinations:

- Send URL to another user's browser ("Open in dev-user's firefox").
- Send page selection to another user's notebook.
- Send current page as an archive to admin's pastebin.
- Send URL to admin's read-later list.

Uses `page.extract` and the cross-user broker. The source user's policy
decides what destinations are offered.

## Cross-user defaults

Given the single-tenant model (admin and users are the same physical
person, data silos are for isolation, not privacy from self):

- **Admin sees everything by default** — tabs, history, downloads, media,
 notifications across all users.
- **Per-user surfaces see only own data.**
- **Admin can enable privacy mode** per user if they want a silo to not
 bleed into admin's aggregate views (e.g., finance-user excluded from
 history search).

Every cross-user surface is visually tagged with the source user's colour
so admin always knows whose data they are looking at.

New integrations default to **own-user-only**. Admin explicitly opts in
for cross-user surfacing — reduces accidental leaks during feature
rollout.

## Configuration per user-browser

The admin panel has a "Browser integration features" page with toggles
per feature per user-browser:

```
work-user / firefox:
 [x] History in launcher
 [x] Bookmarks in launcher
 [ ] Tabs in admin taskbar
 [x] Media controls
 [x] Download notifications
 [x] Screen-lock inhibit
 [x] Web notifications (policy: calendar.google.com, github.com)
 [x] Share-to destinations: dev-user/notebook, admin/readlater
```

## Audit

Every operation is logged by the daemon it terminates at. The admin panel
shows per-user-per-browser activity: operation, timestamp, decision,
identity chain.
