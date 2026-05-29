# qdistro browser desktop daemons (Bridge Phase-9e)

The four SESSION-bus daemons that own the well-known names the browser
bridge's 9e handlers (`qdistro_browser_bridge.py`) call. Before these
shipped, those bus names were unowned and every 9e op returned
`dbus_call_failed`; that also blocked
`todo/browser/02-qdbrowser-unification.md` step-4
(`DownloadsProxy` / `MediaProxy` forwarding).

| Daemon | Bus name | Interface | Op(s) | Module |
|--------|----------|-----------|-------|--------|
| MPRIS | `org.qdistro.Mpris` | `org.qdistro.Mpris1` | `Publish` | `qdistro_mpris_daemon.py` |
| Downloads | `org.qdistro.Downloads` | `org.qdistro.Downloads1` | `Notify` | `qdistro_downloads_daemon.py` |
| Notifications | `org.qdistro.Notifications` | `org.qdistro.Notifications1` | `Show` | `qdistro_notifications_daemon.py` |
| Compositor | `org.qdistro.Compositor` | `org.qdistro.Compositor1` | `ScreenlockInhibit` / `ScreenlockRelease` | `qdistro_compositor_daemon.py` |

## Bus model

These are **per-user session daemons** (`WantedBy=default.target`),
mirroring `qdistro-user-relay`. dbus-broker rejects cross-uid
session-bus peers, so each user owns their own copy. Cross-uid surfacing
happens the standard way:

- **MPRIS** exports `org.mpris.MediaPlayer2.qdistro.<user>.<browser>`
  players; the admin's media widget already aggregates every
  `org.mpris.MediaPlayer2.*` peer it can see.
- **Downloads / Notifications** re-emit through the desktop's own
  `org.freedesktop.Notifications`.
- **Compositor** holds a real `org.freedesktop.login1` idle inhibitor.

## Auth

Every op shares one gate —
`qdistro_browser_daemon_identity.browser_bridge_allowed` — the exact
"executed-script + allowlisted parent-browser exe" check the pwd daemon
uses for browser-credential ops. The caller's uid/pid come from the bus's
SO_PEERCRED view (`GetConnectionUnixUser` / `GetConnectionUnixProcessID`),
never from the request body. The kernel-attested uid keys every per-user
decision (MPRIS player name, download path policy, screen-lock inhibitor
ownership), so a body field can never spoof which user a request belongs
to.

## Testability

Each daemon's dispatch core (`handle_*`) is pure Python over an
injectable sink + identity gate — the same pattern as the bridge's
`_dbus_client`. The `dbus`/`gi` imports live inside `_main()` so the
modules import (and unit-test) with no session bus. See
`tests/unit/test_browser_daemons_9e.py`.

Integration coverage that needs a live browser/VM lives in
`tests/integration/browser_9e_daemons.bats` (marked VM-only).
