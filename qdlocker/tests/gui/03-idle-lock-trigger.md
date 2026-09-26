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
# default 5min (QDLOCKER_IDLE_MS=300000). The `idle.conf` name sorts AFTER the
# GUI-lane `90-ci-gui.conf` dropin (which disables idle for ordinary
# scenarios), so this override wins for THIS test. Only the drop-in is written
# here; the restart that APPLIES it is in Step 1 (see there for why).
"$QDWIN_VM_EXEC" "$VMNAME" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 bash -lc '
  mkdir -p ~/.config/systemd/user/qdlocker.service.d
  cat > ~/.config/systemd/user/qdlocker.service.d/idle.conf <<EOF
[Service]
Environment=QDLOCKER_IDLE_MS=8000
EOF
  systemctl --user daemon-reload
'"
```

## Steps

### Step 1 — apply the 8s threshold and confirm baseline unlocked

The 8s idle timer starts counting the moment the restarted qdlocker binds, and
a keypress only RESETS it — it never unlocks. So the restart, the resetting
keypress and the baseline read must all happen inside one 8s window. (8s,
not 3s, leaves room for the restart and the vm-exec round-trips of this block
— a 3s threshold let the timer fire during setup.)

**Run the whole block below as a SINGLE command** (one tool call; a guest-side
driver must do it in one uninterrupted phase) — do NOT restart qdlocker in one
phase and read the baseline after an agent round-trip or a `*-go` handshake.
In qci run full-20260926T153217Z the driver restarted qdlocker, then waited
15 s (16 s on the retry) for the agent's `setup-go` before the baseline read;
the idle timer had long fired, so 1.1 read `locked=True` on both attempts — a
HARNESS artifact, not a product bug.

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service'
sleep 2
# A lone Shift press (no text side-effect on the focused desktop) restarts the
# 8s idle window at ~0 so it cannot fire during the baseline read below.
qdwin_qmp_key shift down; sleep 0.05; qdwin_qmp_key shift up
qdlocker_ctrl status   # baseline — must be locked=False
qdwin_screenshot /tmp/qdlocker-03-step1-baseline.png
```

**Assert (1.1):** `locked=False`. If the locker came up locked-by-default
(`initially_locked=True` from `qdwin_locker_v1.ready`), this scenario
is meaningless — abort and run after a clean unlock cycle. The screenshot is
supporting evidence only (`qci:visual: none`); it is taken after the status
read.

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
