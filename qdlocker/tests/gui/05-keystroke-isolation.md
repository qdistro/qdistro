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

# Drain stale lock state through the real keyboard unlock path. Do NOT
# "drain" by restarting qdlocker.service: qdwin holds the lock fail-secure
# across a locker restart (journal: `lock held (fail-secure)
# cause=locker_disconnect`; the fresh locker binds with initially_locked=1),
# so the session stays locked. Step 1's Ctrl+Alt+L then lands on an
# already-locked screen: the binding does not fire (the locker's overlay grab
# owns the keyboard). Before qdwin consumed that chord in the overlay grab
# (qdwin_overlay_key_disposition), its `l` reached the locker as Ctrl+L and
# landed in the prompt — how qci runs full-20260929T182319Z and
# full-20260930T212305Z read prompt-len=1 at 1.1, on a retry after the first
# attempt had left the session locked. Step 1 must test a real transition.
qdlocker_drain_lock_state || { echo "ERROR: could not drain a stale lock"; exit 2; }
# Baseline: Step 1 must observe a real unlocked -> locked transition.
case "$(qdlocker_ctrl status 2>/dev/null)" in
    *locked=False*prompt-len=0*) ;;
    *) echo "ERROR: baseline not unlocked/empty: $(qdlocker_ctrl status 2>&1)"; exit 2 ;;
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

# Journal cursor for Step 3.2, scoped to the compositor unit (a whole-journal
# grep through vm-exec would match qemu-ga's own log of the grep command).
JCURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'journalctl -n0 --show-cursor _SYSTEMD_USER_UNIT=qdwin-compositor.service' \
  | sed -n 's/^-- cursor: //p')
[ -n "$JCURSOR" ] || { echo "ERROR: no compositor journal cursor"; exit 2; }
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
prompt-len=0`. The setup baseline was `locked=False`, so this lock is the one
the chord just engaged; nothing of the chord may land in the prompt.
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

```bash
"$QDWIN_VM_EXEC" "$VMNAME" "journalctl -a -o cat --after-cursor='$JCURSOR' \
  _SYSTEMD_USER_UNIT=qdwin-compositor.service | grep 'overlay_key'"
```

**Assert (3.2):** the compositor journal since setup has at least 11
`qdwin: overlay_key role=2 seq=<n> to=locker` lines (routing evidence) and
NO overlay_key line carries key content: no `utf8=`, no `sym=`, and no
`[NNB blob data]` placeholder. qdwin used to log every overlay key's keysym
and utf8, i.e. the typed password in plaintext; a content-bearing line here
is a security regression (`qdwin_overlay_grab_key`, guarded at build time by
`qdwin/qdwin/test_lock_fail_secure.py` `check_overlay_key_log_redacted`).

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

Asserts 1.1, 2.1, 2.2, 3.1, 3.2 must all PASS. 2.2 is the boundary
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
- 1.1 FAIL with `prompt-len > 0` at the lock instant — first make
  sure the baseline really was `locked=False` (setup refuses otherwise).
  On a locked screen qdwin consumes a fresh Ctrl+Alt+L in the overlay
  grab (journal: `overlay_key role=2 lock-hotkey consumed`); if that
  line is missing and the prompt grew by one, the consume regressed
  (`qdwin_overlay_key_disposition`, unit-tested in
  `qdwin/tests/unit/test-qdwin-logic.c`). With a genuine unlocked
  baseline, `prompt-len > 0` means a key reached the locker across the
  lock transition — investigate `qdwin_overlay_grab_start(role=2)`. The
  journal no longer shows WHICH key arrived (overlay_key lines are
  content-free since the password-logging fix); the earlier diagnosis of
  the stray key as Ctrl+L relied on that now-removed leak (a 71-byte
  journald blob = `sym=108 utf8="\x0c"`). To identify a key now, use
  `seq=` counts around `lock_requested` and reproduce interactively.
