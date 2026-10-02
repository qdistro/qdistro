# qdgreeter

Graphical boot greeter for qdistro. Drop-in replacement for `tuigreet`
on the admin TTY, sharing the Python + QML stack and the qdshell
styling story with sibling components
[qdlocker](../qdlocker) and
[qdshell](../qdshell).

## Role in qdistro

qdgreeter owns only the boot login path. It authenticates the single qdistro
owner through greetd/PAM and starts the qdwin session stack. After a successful
login, runtime locking belongs to [qdlocker](../qdlocker); the compositor should
remain locked until qdlocker has authenticated and called the locker protocol.

Keeping greeter and locker separate matches the current session docs: boot
authentication launches a session, while runtime re-auth unlocks an already
running compositor.

## Relationship to qdlocker

Per `qdistro/doc/sessions.md`, the compositor on tty3 starts in
**locked** state — first-boot admin auth and runtime re-auth could
both live in qdlocker. We're splitting them:

| Path        | When                                          | Process    |
|-------------|-----------------------------------------------|------------|
| Boot login  | tty3, before the compositor's first frame     | qdgreeter  |
| Idle / lid / manual lock | Anytime the compositor is running | qdlocker   |

qdgreeter exits after a successful auth and hands off to qdshell +
qdlocker; from that point on, the compositor stays locked until
qdlocker calls `set_locked(0)`.

The split exists so:

- Boot can have welcome / "first run" branding distinct from the
  runtime lock chrome.
- Boot auth can go through `greetd`'s JSON IPC (which handles
  session-creation, environment, PAM auth) without qdlocker
  needing to know anything about session launch.
- A future multi-admin world could swap greeter implementations
  without touching the locker.

## Layout

Same shape as qdlocker:

```
qdgreeter/
├── qdgreeter/
│   ├── app.py           QGuiApplication + QML
│   ├── controller.py    GreetController (greetd JSON client)
│   ├── greetd.py        UNIX-socket JSON protocol to greetd
├── qdgreeter/qml/          (package data: shipped by pip)
│   ├── Main.qml
│   └── GreetUI.qml
├── systemd/
│   └── qdgreeter.service
└── pyproject.toml
```

The QML is intentionally self-contained for the preview build.

## Status

Implemented preview. The greeter has a PyQt/QML app entrypoint, a controller
driving greetd auth flow, and a length-prefixed greetd JSON protocol client in
`qdgreeter/greetd.py`. Unit tests cover the wire format, fake-greetd
round-trips, password prompt handling, retry/cancel, and password log
redaction. Remaining work is packaging/integration hardening and richer
session selection, not the basic greetd client.

## Why not just reuse qdlocker on first boot?

We considered it. The downside is operational: qdlocker would have
to know whether it's "fresh boot" or "runtime re-lock" to pick
branding and decide whether to spawn a session vs. reuse one. The
runtime branch is *much* simpler (PAM auth → set_locked(0)), so
keeping greetd integration in a separate binary preserves that.
