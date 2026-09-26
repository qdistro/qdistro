# 08 — locker crash while locked demotes lock toplevel

<!-- qci:visual: required -->

**Acceptance criterion (resource cleanup):** if qdlocker's control
connection dies while the compositor is locked, qdwin must demote the
promoted lock toplevel from the LOCK layer. The screen stays locked
(fail-safe: no auto-unlock on locker death), but the orphaned toplevel
must not remain on the lock layer with stale identity state.

When a fresh qdlocker binds and maps its Qt window, the new toplevel
is promoted and the lock screen comes back.

This scenario tests the `qdwin_demote_lock_toplevel(...,
"locker_disconnect")` path added in qdwin's `qdwin_locker_resource_destroy`.

## Setup

```bash
source "$(dirname "$0")/qdlocker-helpers.sh"
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdlocker_session_healthy || { echo "FAIL: session not up"; exit 2; }

qdlocker_drain_lock_state
```

## Steps

### Step 1 — engage the locker

```bash
qdlocker_ctrl lock
qdlocker_wait_for_lock 5
qdwin_screenshot /tmp/qdlocker-08-step1-locked.png
qdlocker_ctrl status
```

**Assert (1.1):** `qdlocker_ctrl status` reports `locked=True`.
**Assert (1.2):** screenshot shows qdlocker UI.

### Step 2 — kill qdlocker while locked

```bash
# `runuser -l admin -c` (login shell) is required: a bare `runuser -u admin --
# systemctl --user` lacks XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS, so the
# user-manager lookup fails (rc=1) and the kill is a SILENT no-op.
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "systemctl --user kill --signal=KILL qdlocker.service"'
sleep 0.5
qdwin_screenshot /tmp/qdlocker-08-step2-locker-dead.png
```

**Assert (2.1):** screenshot should show either a transient black screen
(lock toplevel demoted before restart) or a freshly restored qdlocker UI
(systemd restarted the locker quickly). Do not require a fixed 3-second
black frame; under `Restart=always`, the replacement locker can rebind
before the screenshot. The load-bearing demotion proof is Step 3, and the
fail-safe locked-state proof is Step 4/5.

Expect the Step 2 frame to show the **recovered lock UI** more often than
black: systemd rebinds the locker ~2.5 s after the kill, and `vm-gui`
rejects near-black captures as a stale framebuffer and retries, so the frame it
accepts is usually taken after the rebind. Grade the Step 2 frame from its own
pixels — never from what this step "should" show. If Step 5's frame is later
reported as showing the SAME SCREEN PIXELS as Step 2, that means both show the
same (normally recovered) lock screen; describe Step 2's actual content before
drawing any conclusion from that note. (qci run full-20260926T153217Z: both
frames showed the clock + password field, yet the driver described Step 2 as
black and then failed 5.2 as "identical to the black frame".)

### Step 3 — journal confirms demote-on-disconnect

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "journalctl --user -u qdwin-compositor.service --since \"2 minutes ago\" --no-pager"' \
  | grep -E 'demoted locker toplevel.*locker_disconnect'
```

**Assert (3.1):** journal contains `demoted locker toplevel
handle=... via locker_disconnect`. This confirms qdwin ran
`qdwin_demote_lock_toplevel` from the resource destroy handler.

### Step 4 — compositor still reports locked

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "journalctl --user -u qdwin-compositor.service --since \"2 minutes ago\" --no-pager"' \
  | grep 'locker unbound'
```

**Assert (4.1):** journal contains `qdwin: locker unbound` but does
NOT contain `locked_changed=0` after the unbind. The compositor did
not auto-unlock — fail-safe held.

### Step 5 — restart qdlocker and verify recovery

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "systemctl --user restart qdlocker.service"'
sleep 3
qdlocker_session_healthy || { echo "FAIL: qdlocker did not recover"; exit 1; }
qdlocker_ctrl status
qdwin_screenshot /tmp/qdlocker-08-step5-recovered.png
```

**Assert (5.1):** `qdlocker_ctrl status` reports `locked=True` (the
compositor was still locked; the fresh locker inherited that state via
`ready(initially_locked=1)`).
**Assert (5.2):** screenshot shows the qdlocker UI again — the fresh
locker's Qt toplevel was promoted to the lock layer.
**Assert (5.3):** the compositor journal shows the fresh locker's toplevel
promoted AFTER the demotion — a `promoted locker toplevel handle=N to
lock_layer` line whose handle differs from the demoted one and which follows
the `demoted locker toplevel ... via locker_disconnect` line:

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "journalctl --user -u qdwin-compositor.service --since \"2 minutes ago\" --no-pager"' \
  | awk '
      /demoted locker toplevel handle=[0-9]+ via locker_disconnect/ {
          match($0, /handle=[0-9]+/); demoted = substr($0, RSTART + 7, RLENGTH - 7); promoted = ""; next }
      demoted != "" && /promoted locker toplevel handle=[0-9]+ to lock_layer/ {
          match($0, /handle=[0-9]+/); h = substr($0, RSTART + 7, RLENGTH - 7)
          if (h != demoted) promoted = h }
      END {
          if (demoted == "" || promoted == "") {
              print "FAIL 5.3: demoted=" demoted " promoted-after=" promoted; exit 1 }
          print "OK 5.3: demoted handle=" demoted ", later promoted handle=" promoted }'
```

The awk exits non-zero unless the LAST `locker_disconnect` demotion is followed
by a promotion of a DIFFERENT handle.

5.3 is additive corroboration for 5.2; it does not replace the visual check.

### Step 6 — unlock through recovered locker

```bash
qdlocker_unlock_with_password
qdlocker_ctrl status
qdwin_screenshot /tmp/qdlocker-08-step6-unlocked.png
```

**Assert (6.1):** `locked=False`. Normal desktop is visible.

## Cleanup

```bash
qdlocker_drain_lock_state
sleep 1
```

## Pass criteria

All asserts 1.1 → 6.1 pass. Confirms:

- qdwin demotes the lock toplevel on locker disconnect (fix 1).
- The compositor stays locked (fail-safe) when the locker dies.
- A fresh locker bind + Qt toplevel promotion restores the lock screen.
- The full lock/crash/recover/unlock cycle completes.

## Known-broken-if

- Step 3 FAIL (no `locker_disconnect` in journal) — the
  `qdwin_demote_lock_toplevel` call is missing from
  `qdwin_locker_resource_destroy`. This is the exact bug this
  scenario exists to catch.
- Step 2 shows desktop instead of black — qdwin auto-unlocked on
  locker death, violating fail-safe. Check that
  `qdwin_locker_resource_destroy` does NOT call `set_locked(0)`.
- Step 5 shows `locked=False` — the fresh locker's `ready` event
  reported `initially_locked=0`, meaning the compositor lost its
  locked state during the crash. Same root cause as above.
