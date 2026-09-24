# qdlocker

Screen locker for [qdistro](../README.md). Qt/QML UI driven by a Python
controller, talking to [qdwin](../qdwin) over the
`qdwin_locker_v1` private Wayland protocol.

## Role in qdistro

qdlocker owns runtime re-authentication for the whole machine. qdistro is
single-tenant, so one lock covers every silo and session surface on the active
compositor. qdlocker authenticates the owner through fprintd/PAM and then asks
qdwin to demote the lock layer.

Boot login is deliberately separate and belongs to [qdgreeter](../qdgreeter).
The production session should treat qdlocker as part of the qdwin session stack,
not as optional shell chrome.

A sibling component to [qdshell](../qdshell), not part of it. The original
locker lived inside qdshell as `Modules/LockScreen/*.qml`; this component lifts
it out so the locker's process lifecycle is independent of the shell
(a shell crash no longer drops the screen unlocked, and a locker
crash doesn't take chrome down with it).

## Architecture

```
   ┌──────────────────────────────────────────────────────────┐
   │                       qdwin (C)                          │
   │                                                          │
   │   qdwin_shell_v1   ──── allowed_uid filter ──── qdshell  │
   │                                                          │
   │   qdwin_locker_v1  ──── allowed_locker_uid ──── qdlocker │
   │                                                          │
   │   shared LOCK layer + state machine (qdwin.c:4946+)      │
   └──────────────────────────────────────────────────────────┘
```

The locker:

- Binds `qdwin_locker_v1` as the sole locker client.
- Renders the lock UI into a `QQuickWindow`; qdwin identifies that
  Qt toplevel as belonging to the authorized locker process and
  promotes it to the LOCK layer while locked.
- Calls `set_locked(1)` on lock trigger, `set_locked(0)` after auth.
- Receives keystrokes via `overlay_key` (qdwin grabs the keyboard
  while locked so the password text never reaches qdshell or any
  user app).
- Auths via `fprintd` on the system bus and `python-pam` as the
  password fallback (mirrors `qdistro/doc/sessions.md:39-40`).

Automatic locking subscribes to logind's manager-wide `PrepareForSleep`
signal and holds a sleep delay inhibitor until qdwin confirms the lock.
`lid_action=ignore` disables only lid-triggered locking. A systemd user
service without a PID-associated session still protects suspend; lid
signals use an owned session resolved through `XDG_SESSION_ID`, or one
unambiguous active local Wayland seat session when that identifier is stale.

The watcher reconnects after bus/logind loss and session replacement,
cancelling old callbacks and pending lock-confirmation tasks before taking
a new inhibitor. `LogindWatcher.automatic_lock_ready` reports whether the
sleep subscription has an inhibitor; the journal identifies degraded and
restored automatic-lock readiness. Manual and compositor idle locking
remain independent of logind readiness.

The reciprocal compositor wiring is documented in
[`../qdwin/doc/locker.md`](../qdwin/doc/locker.md).

## Layout

```
qdlocker/
├── qdlocker/             Python package
│   ├── app.py            QGuiApplication + QQmlApplicationEngine entry
│   ├── controller.py     LockController (Python mirror of LockContext.qml)
│   ├── auth.py           fprintd D-Bus + PAM
│   ├── wayland.py        qdwin_locker_v1 client
│   ├── idle.py           ext-idle-notify-v1 subscription
│   └── keysyms.py        XKB keysym constants
├── qml/
│   ├── Main.qml          Root QQuickWindow
│   └── LockUI.qml        Visual layout (reuses qs.Commons + qs.Widgets)
├── protocol/
│   └── qdwin-locker-v1.xml   Vendored copy of the protocol XML
├── systemd/
│   └── qdlocker.service  User unit
├── tests/
│   ├── gui/              VM tests (virsh + QMP, mirrors qdwin/tests/gui/)
│   └── unit/             pytest controller/auth tests
└── pyproject.toml
```

## Styling — mirrors qdshell

The QML imports only the in-tree `shim` module (`qml/shim/Style.qml`,
`qml/shim/Color.qml`): Quickshell-free singletons whose property names and
default values mirror [qdshell](../qdshell)'s `Commons/Style.qml` and
`Commons/Color.qml`, so the locker looks like the shell without depending on
it. No QML file imports `qs.*`. `app.py:_qdshell_import_path` still adds
qdshell (found at `../qdshell`, or `QDLOCKER_QDSHELL_PATH=/path/to/qdshell`)
to the QML import path, but that is only a hook: nothing uses it today.

## Run

### Dev mode (no compositor)

```bash
QDLOCKER_NO_WAYLAND=1 \
QDLOCKER_QDSHELL_PATH=$(pwd)/../qdshell \
python -m qdlocker
```

The UI comes up in a regular Wayland window; useful for iterating on
the QML layout without restarting qdwin.

### Real session

Install the systemd user unit and start it after qdwin/qdshell:

```bash
install -m 644 systemd/qdlocker.service ~/.config/systemd/user/
systemctl --user enable --now qdlocker.service
```

## VM testing — same harness as qdwin

The tests under `tests/gui/` are literate Markdown scenarios driven
by the same primitives qdwin uses (virsh send-key for keyboard, QMP
input-send-event for chords with modifier discipline, `virsh
screenshot` for visual asserts, qdshell ctrl-socket for state
introspection, qdlocker's own `--ctrl-socket` for locker state).

Quickstart:

```bash
# Build a qdistro guest image with qdlocker installed (see
# tier4-vm/build-guest-image.sh at the monorepo root):
$ ../tier4-vm/build-guest-image.sh --include qdlocker

# Launch it:
$ ../tier4-vm/spawn-tier4.sh

# Drive the 01-lock-cycle scenario:
$ cd tests/gui
$ source qdlocker-helpers.sh
$ qdwin_set_vm "$(virsh -c qemu:///session list --name --state-running | head -1)"
$ bash run-scenario.sh 01-lock-cycle.md
```

The harness lives in `tests/gui/qdlocker-helpers.sh`; it sources
`qdwin-helpers.sh` from the qdwin sibling and adds:

- `qdlocker_ctrl <command>` — talks to qdlocker's ctrl socket
  (`/run/user/<uid>/qdlocker.sock`) to introspect state (locked
  flag, prompt buffer length, last error).
- `qdlocker_wait_for_unlock` — polls until the controller reports
  `unlocked`, with a default 5s timeout.

## Status

Implemented preview:

- ✅ Protocol XML + meson generation in qdwin
- ✅ Python controller + auth backend (fprintd + PAM)
- ✅ QML UI reusing qdshell styling
- ✅ systemd unit + README
- ✅ VM test scaffolding
- ✅ qdwin C-side `bind_qdwin_locker` + locker resource handlers
      (see qdwin/doc/locker.md)
- ✅ pywayland scanner output committed under `protocol/`
- ✅ Ctrl-socket implementation for test introspection (`qdlocker/ctrl.py`),
      wired from `app.py` after the QML root window is ready

See `tests/gui/01-lock-cycle.md` for the acceptance criterion.
