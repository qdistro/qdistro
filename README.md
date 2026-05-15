# qdlocker

Screen locker for [qdistro](../qdistro). Qt/QML UI driven by a Python
controller, talking to [qdwin](../qdwin) over the
`qdwin_locker_v1` private Wayland protocol.

Sibling to [qdshell](../qdshell), not part of it. The original locker
lived inside qdshell as `Modules/LockScreen/*.qml`; this repo lifts
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
- Renders the lock UI into a `QQuickWindow` whose `wl_surface` is
  attached as the LOCK-layer surface via
  `qdwin_locker_v1.attach_lock_surface`.
- Calls `set_locked(1)` on lock trigger, `set_locked(0)` after auth.
- Receives keystrokes via `overlay_key` (qdwin grabs the keyboard
  while locked so the password text never reaches qdshell or any
  user app).
- Auths via `fprintd` on the system bus and `python-pam` as the
  password fallback (mirrors `qdistro/doc/sessions.md:39-40`).

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

## Styling — reuses qdshell

The QML imports `qs.Commons` (Style.qml, Color.qml, Icons.qml) and
`qs.Widgets` (NText, NIcon, NIconButton, NBusyIndicator) from the
qdshell sibling repo. `app.py:_qdshell_import_path` finds qdshell
automatically when the two repos are siblings; override with
`QDLOCKER_QDSHELL_PATH=/path/to/qdshell` if you've laid things out
differently. The locker falls back to inline rendering for the
header if qdshell isn't on the path.

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
# qdistro/tier4-vm/build-guest-image.sh):
$ ../qdistro/tier4-vm/build-guest-image.sh --include qdlocker

# Launch it:
$ ../qdistro/tier4-vm/spawn-tier4.sh

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

This is the initial scaffold:

- ✅ Protocol XML + meson generation in qdwin
- ✅ Python controller + auth backend (fprintd + PAM)
- ✅ QML UI reusing qdshell styling
- ✅ systemd unit + README
- ✅ VM test scaffolding
- ⏳ qdwin C-side `bind_qdwin_locker` + locker resource handlers
      (see qdwin/doc/locker.md for the recipe — the existing
      shell-side `attach_lock_surface` body is reusable)
- ⏳ pywayland scanner output committed under `protocol/` (run
      `pywayland-scanner` against `qdwin-locker-v1.xml`)
- ⏳ Ctrl-socket implementation in app.py for test introspection

See `tests/gui/01-lock-cycle.md` for the acceptance criterion.
