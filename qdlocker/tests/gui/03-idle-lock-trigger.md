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

# Drain a stale locked state (a prior scenario, or this scenario's own earlier
# attempt whose idle timer fired) through the real keyboard unlock path.
# Restarting qdlocker does NOT unlock: qdwin holds the lock fail-secure across
# a locker restart and the fresh locker binds with initially_locked=1.
qdlocker_drain_lock_state || { echo "ERROR: could not drain a stale lock"; exit 2; }

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

The 8s idle timer starts counting the moment the restarted qdlocker binds:
qdwin runs ext-idle-notify in internal-idle mode (`weston.ini idle-time=0`;
journal `ext-idle-notify idle_time=0 internal_mode=1`), which arms each
notification's timer for the full timeout at creation
(`qdwin_idle_notification_create`, qdwin.c). So the restart itself opens a
fresh 8s window and the baseline read needs NO keypress — only that the
restart and the read happen in ONE uninterrupted guest command.

**Run the block below as a SINGLE guest-side command** (one vm-exec, or one
uninterrupted phase of a guest driver). There is NO host keyboard action in
this step: do NOT put a `qci_host_step` / `*-go` handshake (or any agent
round-trip) between the restart and the status read. In qci run
full-20260930T212305Z the guest driver restarted qdlocker, then blocked on a
host step for a Shift press; the agent's round-trip took >8s, the idle timer
fired at restart+8.0s (qdlocker journal `idle threshold reached`) before the
Shift (restart+9.6s), and 1.1 read `locked=True` on both attempts — a HARNESS
artifact, not a product bug. (run full-20260926T153217Z failed the same way.)

```bash
"$QDWIN_VM_EXEC" "$VMNAME" '
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service
  sleep 2
  printf "status\n" | runuser -u admin -- socat -t 1 - UNIX-CONNECT:/run/user/1000/qdlocker.sock'
# ^ baseline — must be locked=False (read ~2 s into the 8 s window)
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

**Run the whole block below as a SINGLE HOST-side command** — do NOT split the
unlock, the activity keypress, and the status read across separate tool calls.
The idle timer keeps ticking between calls, so a multi-second gap between the
unlock and the activity keypress lets the 8s window re-fire and re-lock before
the check, producing a spurious `locked=True` that is a HARNESS artifact, not a
product bug. With a guest-side driver this block is ONE host step whose host
side performs the status read itself (via vm-exec) and records it; the guest
must NOT do the 3.1 read after the step's `go` — the agent round-trip before
the `go` can exceed 8s, exactly the Step 1 failure mode.

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
