# 05 — keystrokes typed while locked DO NOT reach qdshell

<!-- qci:visual: none -->

**Acceptance criterion (security):** while qdlocker is engaged,
keyboard `overlay_key` events route to `qdwin_locker_v1` only — they
do NOT reach `qdwin_shell_v1`. Said differently: the password the
user types into the lock screen never enters qdshell's process
memory.

This is the load-bearing reason the locker is its own peer process
on its own private protocol. If this assertion fails, the lock UI
is theatre.

qdshell exposes a `qs ipc call qdwin lastOverlayKeys` accessor that
returns the count of `overlay_key` events qdshell has received since
boot. We assert that count does NOT advance during a locked typing
sequence, even as qdlocker's `prompt-len` does.

## Setup

```bash
source "$(dirname "$0")/qdlocker-helpers.sh"
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdlocker_session_healthy || { echo "FAIL: session not up"; exit 2; }

# Drain stale lock state.
case "$(qdlocker_ctrl status 2>/dev/null)" in
    *locked=True*)
        "$QDWIN_VM_EXEC" "$VMNAME" \
          'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service; sleep 2' \
          >/dev/null
        ;;
esac

# The qdshell-side counter for overlay_key receipts is the load-
# bearing assertion of this scenario. It MUST be a real, numeric,
# advanced-on-each-event counter, NOT a placeholder. We assert that
# the read returns a non-empty integer before the test proceeds —
# without this, a missing/typo'd command would return empty strings
# and the equality assertion would silently green-pass.
SHELL_BASELINE=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 qs ipc -p /usr/share/quickshell/qdshell call qdwin lastOverlayKeys' \
  | sed -n 's/.*count=\([0-9]\+\).*/\1/p')
if ! [[ "$SHELL_BASELINE" =~ ^[0-9]+$ ]]; then
    echo "ERROR: qs ipc call qdwin lastOverlayKeys did not return 'count=<int>' — got: $SHELL_BASELINE" >&2
    exit 78  # bats: hard ERROR (not SKIP), this is a security regression risk
fi
echo "shell overlay_key baseline=$SHELL_BASELINE"
```

## Steps

### Step 1 — engage the locker

```bash
qdwin_chord ctrl alt -- l
qdlocker_wait_for_lock 5
qdlocker_ctrl status
SHELL_AFTER_LOCK=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 qs ipc -p /usr/share/quickshell/qdshell call qdwin lastOverlayKeys' \
  | sed -n 's/.*count=\([0-9]*\).*/\1/p')
echo "shell overlay_key after-lock=$SHELL_AFTER_LOCK"
```

**Assert (1.1):** `qdlocker_ctrl status` reports `locked=True
prompt-len=0`.
**Assert (1.2):** `$SHELL_AFTER_LOCK == $SHELL_BASELINE` — engaging
the locker MUST NOT cause any overlay_key delivery to qdshell.
Earlier drafts allowed ≤2 to absorb test flakiness; we changed to
strict equality after a transition-window leak slipped through. If
this fails, the lock-transition routing in qdwin.c
(`qdwin_overlay_grab_start(role=2)`) is racing with the
`set_locked(1)` state flip.

### Step 2 — type the test password

```bash
qdlocker_type_password_chars
sleep 0.3
qdlocker_ctrl status
SHELL_AFTER_TYPING=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 qs ipc -p /usr/share/quickshell/qdshell call qdwin lastOverlayKeys' \
  | sed -n 's/.*count=\([0-9]\+\).*/\1/p')
# Same numeric-shape guard as setup — defends against silent green-pass
# if the command starts returning errors mid-test (qdshell restart,
# socket disconnect).
if ! [[ "$SHELL_AFTER_TYPING" =~ ^[0-9]+$ ]]; then
    echo "FAIL (3.x precondition): qdwin lastOverlayKeys returned non-numeric: $SHELL_AFTER_TYPING" >&2
    exit 1
fi
echo "shell overlay_key after-typing=$SHELL_AFTER_TYPING"
```

**Assert (2.1):** `qdlocker_ctrl status` reports `prompt-len=11`. The
locker received the full test password.

**Assert (2.2):** `$SHELL_AFTER_TYPING` == `$SHELL_AFTER_LOCK`. The
qdshell overlay_key counter has NOT advanced. **This is the
load-bearing assertion of the entire suite.** If the counter
advanced, qdshell received the password characters — the protocol's
overlay-key router is delivering to the wrong resource.

### Step 3 — masked introspection only

```bash
qdlocker_ctrl prompt-text
```

**Assert (3.1):** the response is `masked=*********** len=11` — eleven
asterisks, no plaintext. The locker's ctrl socket must NOT leak the
buffer even to a privileged test caller. (Tests would otherwise
become a documented exfiltration path.)

### Step 4 — cleanup unlock

```bash
qdwin_send_key KEY_ENTER
qdlocker_wait_for_unlock 5
```

**Assert (4.1):** `qdlocker_ctrl unlock-result` reports `last=success`.

## Cleanup

```bash
# Nothing to do beyond unlock above.
true
```

## Pass criteria

Asserts 1.1, 2.1, 2.2, 3.1 must all PASS. 2.2 is the boundary
assertion; a FAIL here is a release-blocker security regression.

## Known-broken-if

- 2.2 FAIL with `SHELL_AFTER_TYPING` advanced by 6 — qdwin's
  overlay_key router is sending to the shell resource regardless of
  whether the locker is bound. The fix is in qdwin.c overlay-key
  dispatch: check `qdwin->locker_resource != NULL &&
  qdwin->locked` before falling through to the shell. See
  `qdwin/doc/locker.md §5`.
- 2.2 FAIL with `SHELL_AFTER_TYPING` advanced by some smaller number
  (e.g. 1 or 2) — keystroke leakage on transition edges. Probably
  the first key or two arrive before qdwin notices the lock
  transition. Tighten the order in `set_locked(1)` so the keyboard
  grab is installed before the event returns.
- 3.1 FAIL with the literal password in the response — `ctrl.py`'s
  `prompt-text` handler is returning `self._controller.currentText`
  directly. It must mask.
- 1.1 FAIL with `prompt-len > 0` at the lock instant — a residual
  buffer from a prior test wasn't cleared on lock. Setup must drain
  the controller; the easiest is the
  `systemctl --user restart qdlocker.service` path.
