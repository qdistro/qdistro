# Smoke run — 2026-05-15

First end-to-end run against a fresh qdistro tier4 VM. Validates the
qdwin C-side `qdwin_locker_v1` wiring without depending on the Qt+QML
UI layer (the Qt-Wayland integration on this image needs separate
work — see "Known gaps" below).

## VM

- Domain: `qdlocker-smoke-260515-0728` (clone of `baseweed-baked.qcow2`)
- Spawned via `qdistro/scripts/vm/spin-test-vm.sh qdlocker-smoke`
- qdwin compiled in-VM via `fresh-vm-bootstrap.sh` from sibling tarballs

## What the smoke validates

| # | Smoke assertion | Result | Where checked |
|---|---|---|---|
| 1 | `qdwin_locker_v1` global advertised by qdwin | PASS | probe registry listener |
| 2 | `bind_as_locker` accepted from admin uid | PASS | probe `ready` event + qdwin journal `qdwin: locker bound (initially_locked=0)` |
| 3 | `set_locked(1)` is accepted before an explicit lock surface exists | PASS | probe step [4] |
| 4 | `set_locked(1)` → `locked_changed(1)` | PASS | probe + journal `locked_changed=1 cause=locker_set_locked` |
| 5 | `set_locked(0)` → `locked_changed(0)` | PASS | probe + journal `locked_changed=0` |
| 6 | Ctrl+Alt+L hotkey → `lock_requested(reason=3=manual)` reaches locker | PASS | probe `[('lock_requested', 3)]` + journal `qdwin: lock_requested` |
| 7 | **Security boundary**: typed-while-locked keystrokes route to locker via `overlay_key`, NOT to qdshell | **PASS** | probe reassembled the test password from overlay events + journal `qdwin: overlay_key role=2 sym=… utf8="x" state=PRESSED` for each char |

Scenarios 03 (idle), 04 (lid/suspend), 06 (shell crash) — not
exercised in this run. Note that 03 (idle, via `ext-idle-notify-v1`)
and 04 (lid/suspend, via qdlocker's own `LogindWatcher` subscribing to
logind `Session.Lock`/`PrepareForSleep`) do NOT go through the qdwin
`qdwin_locker_v1` wiring this smoke validates — only the manual hotkey
(reason=3) and compositor lock state do.

## How it was driven

Pure-pywayland probes (no Qt) under
`/tmp/locker-probe{,2,3}.py` + `/tmp/locker-probe-keys.py` in the
guest. Each probe:

1. Connects to `wayland-1`
2. Registers a global listener
3. Binds `qdwin_locker_v1`
4. Calls `bind_as_locker`
5. Asserts the relevant events arrive

Host injects keys via `virsh send-key --codeset linux KEY_*`.

## qdwin C changes proven by this run

All edits in `qdwin/qdwin/qdwin.c` for the new protocol:

- `struct qdwin` fields: `locker_global`, `locker_resource`,
  `allowed_locker_uid` — populated correctly
- `wl_global_create(qdwin_locker_v1_interface, 1, ...)` — global
  registered and visible to clients (assertion 1)
- `bind_qdwin_locker` — uid filter + resource setup (assertion 2)
- `qdwin_handle_bind_as_locker` — emits `ready` (assertion 2)
- `qdwin_handle_locker_set_locked` — drives compositor lock state,
  hides normal layers, starts overlay grab role=2, and fans
  `locked_changed` to both shell and locker resources (assertions 3-5 + 7)
- Lock-key hotkey fan-out — `qdwin_locker_v1_send_lock_requested`
  on the locker resource (assertion 6)
- Overlay-key router — `if (overlay_grab_role == 2 && locker_resource)`
  routes to `qdwin_locker_v1_send_overlay_key`, bypassing the shell
  (assertion 7, the security boundary)

## Known gaps surfaced by this run

1. **Qt-Wayland integration on the baked image needs work.** A
   minimal `QGuiApplication()` aborts inside the VM. PySide6 6.11 +
   qt6-wayland-imports installed, env (WAYLAND_DISPLAY etc.) set
   correctly, but the Wayland connection trips up before `exec()`.
   This blocks the QML lock UI from rendering. The smoke probes
   bypass this by talking the protocol directly with pywayland.
   Tracked: build the locker UI on a Qt-on-Wayland baseweed
   variant, or ship a small native shim.

2. **qdshell QML modules (`qs.Commons`, `qs.Widgets`) require
   Quickshell.** Reusing them from plain Qt QML hits transitive
   imports of `Quickshell`, `Quickshell.Widgets`, `qs.Services.UI`.
   Either qdlocker UI hosts inside quickshell, or qdshell widgets
   get a Quickshell-free shim. Tracked separately; smoke uses a
   stub LockUI.qml in the meantime.

3. **Pre-existing `qdwin_activation_token_free` segfault.** Every
   probe exit triggers a SIGSEGV in qdwin's xdg-activation cleanup
   path (`wl_list_remove` on a dangling node). Not introduced by
   the locker changes — affects every Wayland client disconnect.
   File: `qdwin/qdwin/qdwin.c` around `qdwin_activation_token_free`.

4. **`install-pwd-for-vm.sh` failure.** Bootstrap stage 4 hits a
   PAM/PWD daemon startup failure that aborts the rest of the
   bootstrap. Worked around by re-running the remaining install
   scripts manually. Unrelated to qdlocker.

## Reproducing

```bash
# From qdistro-org/qdistro:
bash scripts/vm/spin-test-vm.sh qdlocker-smoke

# After spin-test-vm finishes, inside the VM:
runuser -l admin -c 'WAYLAND_DISPLAY=wayland-1 XDG_RUNTIME_DIR=/run/user/1000 \
    python3 /tmp/locker-probe-keys.py'
# Then from the host:
for k in K R U G E R; do
    virsh -c qemu:///session send-key <vm> --codeset linux --holdtime 30 KEY_$k
    sleep 0.05
done
```
