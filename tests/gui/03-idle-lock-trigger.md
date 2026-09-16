# 03 — idle timer engages the locker

<!-- qci:visual: none -->

**Acceptance criterion:** after `QDLOCKER_IDLE_MS` of no keyboard or
pointer activity, qdlocker engages the lock via its
`ext-idle-notify-v1` subscription. The compositor's
`lock_requested(reason=0=idle)` event is *not* used here — the locker
watches idle directly so the timeout is reconfigurable without a
compositor restart (per `qdwin/qdwin/qdwin-shell-v1.xml:666-680`
on the shell side, mirrored for the locker).

## Setup

```bash
source "$(dirname "$0")/qdlocker-helpers.sh"
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdlocker_session_healthy || { echo "FAIL: session not up"; exit 2; }

# Drain a stale locked state from a prior scenario.
case "$(qdlocker_ctrl status 2>/dev/null)" in
    *locked=True*)
        "$QDWIN_VM_EXEC" "$VMNAME" \
          'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service; sleep 2' \
          >/dev/null
        ;;
esac

# Shorten the idle threshold so the scenario doesn't wall-clock wait for the
# default 5min (QDLOCKER_IDLE_MS=300000). 8s — NOT 3s — is deliberate: the
# setup restart + vm-exec round-trips can take >3s, so a 3s threshold let the
# idle timer fire and lock the screen DURING setup, before the baseline check
# could read the unlocked state (the test raced itself). 8s is comfortably
# larger than the setup+round-trip budget yet still short enough to exercise.
# The `idle.conf` name sorts AFTER the GUI-lane `90-ci-gui.conf` dropin (which
# disables idle for ordinary scenarios), so this override wins for THIS test.
"$QDWIN_VM_EXEC" "$VMNAME" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 bash -lc '
  mkdir -p ~/.config/systemd/user/qdlocker.service.d
  cat > ~/.config/systemd/user/qdlocker.service.d/idle.conf <<EOF
[Service]
Environment=QDLOCKER_IDLE_MS=8000
EOF
  systemctl --user daemon-reload
  systemctl --user restart qdlocker.service
'"
sleep 2

# Reset the idle counter immediately before reading the baseline: a lone Shift
# press (no text side-effect on the focused desktop) generates input so the 8s
# idle window restarts at ~0 and cannot fire during the baseline read below.
qdwin_qmp_key shift down; sleep 0.05; qdwin_qmp_key shift up
qdlocker_ctrl status   # baseline — must be locked=False
```

## Steps

### Step 1 — confirm baseline unlocked

```bash
qdlocker_ctrl status
qdwin_screenshot /tmp/qdlocker-03-step1-baseline.png
```

**Assert (1.1):** `locked=False`. If the locker came up locked-by-default
(`initially_locked=True` from `qdwin_locker_v1.ready`), this scenario
is meaningless — abort and run after a clean unlock cycle.

### Step 2 — wait 9s without touching the keyboard/pointer

```bash
sleep 9
qdlocker_ctrl status
qdwin_screenshot /tmp/qdlocker-03-step2-idle.png
```

**Assert (2.1):** `qdlocker_ctrl status` reports `locked=True`. The
idle subscription fired at 8s; the locker entered locked state by
the 9s mark.
**Supporting check (2.2):** capture the screenshot as visual evidence. If
`qdlocker_ctrl status` reports `locked=True`, do not fail this scenario solely
because the screenshot is black, on the wrong VT, or does not show the clock /
password field; the controller state and the Step 3 unlock/reset check are the
load-bearing proof that the idle lock engaged.

### Step 3 — keypress resets the idle counter

This is a regression guard against an idle subscription that doesn't
reset on activity. Ctrl+Alt+L is **lock only**, not a toggle — to
unlock for the next part of the test, send the password through
qdlocker's overlay_key channel using the same pattern as scenario
01 step 3+4.

**Run the whole block below as a SINGLE command** — do NOT split the unlock,
the activity keypress, and the status read across separate tool calls. The idle
timer keeps ticking between calls, so a multi-second gap between the unlock and
the activity keypress lets the 8s window re-fire and re-lock before the check,
producing a spurious `locked=True` that is a HARNESS artifact, not a product bug.

```bash
qdlocker_unlock_with_password

# Now generate activity, then check at t=2s (well under the 8s threshold).
qdwin_qmp_key spc down; sleep 0.05; qdwin_qmp_key spc up
sleep 2
qdlocker_ctrl status
```

**Assert (3.1):** `locked=False` — at t=2s after a keypress, the
idle subscription should NOT have fired (threshold is 8s, last
activity was 2s ago). If `locked=True`, the locker is using
wall-clock since boot instead of last-activity time.

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 bash -lc '
  rm -f ~/.config/systemd/user/qdlocker.service.d/idle.conf
  systemctl --user daemon-reload
  systemctl --user restart qdlocker.service
'"
```

## Known-broken-if

- Step 2 PASS at screenshot but `qdlocker_ctrl` reports
  `locked=False` — the idle subscription fired but qdlocker never
  called `set_locked(1)`. Check `idle.py:start` is wired (the
  scaffold has a TODO).
- Step 2 FAIL at the 9s mark with `locked=False` — `idle.py` isn't
  bound to ext-idle-notify-v1 at all. `idle_watcher.on_idle(...)` is
  set but `idle_watcher.start(display)` was never called.
- Step 3 FAIL with `locked=True` at t=2s — the idle subscription is
  using compositor uptime instead of last-activity. The
  ext-idle-notify-v1 protocol resets the counter on input; the
  client doesn't have to do anything special, so this would mean
  qdlocker is doing its own (wrong) wall-clock timer in parallel.
