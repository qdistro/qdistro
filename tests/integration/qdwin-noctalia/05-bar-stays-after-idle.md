# 05 — bar still visible after DPMS wake

**Acceptance criterion:** after qdwin DPMSes the screen on idle
(armed here to the 1-minute minimum via the qdshell `power.displayOff*`
policy) and the test wakes it via mouse motion, the Noctalia bar
reappears at its original position with no protocol errors during the
wake transition.

This exercises:
- Configure/ack handling on output power-cycle
- weston's DPMS-on path through layer-shell surfaces
- Idle-notify-v1 + idle-inhibit-v1 interaction on Noctalia's bar

This is a regression-detection test — the configure/ack code paths
in qdwin (+1.2) need to handle re-mapping after the
output cycles, which is a different code path from initial mapping.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
source ${QDISTRO_REPO}/tests/integration/qdwin-noctalia/noctalia-helpers.sh
qdwin_set_vm "${VMNAME:-noctalia-vis-260503-1021}"
noct_session_healthy || { echo "FAIL: noctalia not healthy"; exit 1; }

# Arm DPMS display-off for testing. qdwin observes input idle via
# ext-idle-notify-v1; the SHELL (qdshell — the dir name `qdwin-noctalia` is
# historical) owns the idle *timing* via `Settings.data.power.displayOff{AC,
# Battery}` and the compositor enacts display power via the v26 `set_display_
# power` request. The live settings file is ~/.config/qdshell/settings.json
# (shellName=qdshell; the deploy sets no NOCTALIA_CONFIG_DIR). These values are
# in MINUTES — 1 is the smallest non-zero arm (IdlePolicy._toMs), so this test
# runs in ~80s, not ~15s. weston.ini [core] idle-time and the retired Noctalia
# `idle.screenOffTimeout` key are NOT read by qdshell. The proven path is
# qdwin/tests/gui/agent-idle-dpms-recovery-smoke.sh + 20-idle-dpms.md.
ADMIN_USER=$("$QDWIN_VM_EXEC" "$VMNAME" 'getent passwd 1000 | cut -d: -f1' 2>/dev/null | tail -1)
[ -n "$ADMIN_USER" ] || { echo "FAIL: no uid-1000 user in guest (getent passwd 1000 empty)"; exit 1; }
SETTINGS=/home/$ADMIN_USER/.config/qdshell/settings.json

# qs_ipc <method> — proven-working qdwin IPC invocation (same as 16/17/19/20),
# with a PID fallback when the -p path lookup can't find the instance.
QS_PATH=/usr/share/quickshell/qdshell
qs_ipc() {
    local out
    out=$("$QDWIN_VM_EXEC" "$VMNAME" \
        "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
         qs ipc -p $QS_PATH call qdwin $*" 2>&1)
    if printf '%s' "$out" | grep -qiE 'no running instance|No such'; then
        local pid
        pid=$("$QDWIN_VM_EXEC" "$VMNAME" \
            "pgrep -u admin -f '[q]s -p $QS_PATH' | while read p; do \
               grep -q dbus-run-session /proc/\$p/cmdline 2>/dev/null || { echo \$p; break; }; done")
        [ -n "$pid" ] || { printf '%s\n' "$out"; return 1; }
        out=$("$QDWIN_VM_EXEC" "$VMNAME" \
            "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
             qs ipc --pid $pid call qdwin $*" 2>&1)
    fi
    printf '%s\n' "$out"
}

# Capability gate (tri-state, so a broken IPC can't masquerade as a clean skip):
# DPMS display-off is live only on a v26+ shell bind (set_display_power) WITH an
# ext_idle_notifier_v1 + wl_seat, surfaced as CapabilityService.idleDpms.
#  - IPC never reaches a bound shell      -> FAIL (precondition/ERROR, like 20.A.0)
#  - bound shell, but idleDpms != true    -> SKIP (genuine capability gap, e.g.
#                                            an older baked image; not a product FAIL)
#  - bound shell + idleDpms=true          -> proceed
CAPS=
BOUND=0
for _ in $(seq 1 30); do
    CAPS=$(qs_ipc capabilities)
    case "$CAPS" in *bound=true*) BOUND=1; break ;; esac
    sleep 1
done
echo "capabilities: $CAPS"
[ "$BOUND" = 1 ] || {
    echo "FAIL: qdshell IPC never reported bound=true (binding unreachable; got: $CAPS)"
    exit 1
}
case "$CAPS" in
    *idleDpms=true*) ;;
    *)
        echo "SKIP: qdwin idle/DPMS capability unavailable on this image (need v26+ idleDpms=true; got: $CAPS)"
        exit 0
        ;;
esac

# Create-or-edit at the real qdshell path. A fresh VM may have no settings.json
# yet, so load-or-default and write only the power keys (displayOff in MINUTES;
# leave inactivity at 0 so only DPMS arms, never suspend/lock).
"$QDWIN_VM_EXEC" "$VMNAME" "
 install -d -m 700 /home/$ADMIN_USER/.config/qdshell
 python3 - <<PY
import json
p='$SETTINGS'
try:
    d=json.load(open(p))
except (FileNotFoundError, ValueError):
    d={}
pw=d.setdefault('power',{})
pw['displayOffAC']=1
pw['displayOffBattery']=1
pw['presentationMode']=False
json.dump(d, open(p,'w'), indent=2)
PY
 chown -R '$ADMIN_USER:' /home/$ADMIN_USER/.config/qdshell
"
# Cursor BEFORE the restart so the arm-line check proves THIS restart armed the
# policy, not a stale prior arm line inside a --since window.
"$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n0 --show-cursor 2>/dev/null | sed -n 's/^-- cursor: //p' > /tmp/05-arm.cur"
noct_restart

# Fail fast (don't burn the 75s idle wait) if the policy didn't actually arm:
# the proven smoke asserts the same 'idle policy armed: ... displayOff=60000ms'
# journal line. Wait (bounded) for it AFTER the restart cursor; a missing line
# means the write-path/schema is wrong, not that DPMS is slow.
"$QDWIN_VM_EXEC" "$VMNAME" 'source /tmp/qci-gui-waiters.sh
cur=$(cat /tmp/05-arm.cur 2>/dev/null)
[ -n "$cur" ] || { echo "FAIL: missing pre-restart journal cursor"; exit 1; }
await_journal_line_after_cursor "$cur" "idle policy armed:.*displayOff=60000ms" 15 1 _UID=1000' \
    || { echo "FAIL: qdshell did not arm displayOff=60000ms after settings write (schema/path regression?)"; exit 1; }
```

## Steps

### Step 1 — capture awake baseline

```bash
noct_screenshot_awake /tmp/05-step1-awake.png
```

**Assert (1.1):** bar visible in top 31 px (same check as 01.1.2).

### Step 2 — wait for DPMS-off

> **Driver note (MUST):** this is a synchronous ~75 s idle wait. Run the block
> as a single blocking invocation in this turn — do NOT background it, schedule
> a wakeup, or end the session to "check back later". Inject NO input during the
> wait (any pointer/key activity resets ext-idle-notify and re-arms the timer).

```bash
# Cursor before the idle wait so Step 4's clean-log + wake-remap checks scope to
# THIS idle/wake cycle, not a stale line in a --since '1 minute ago' window.
"$QDWIN_VM_EXEC" "$VMNAME" \
  "runuser -l admin -c \"journalctl --user -u qdwin-compositor.service -n0 --show-cursor 2>/dev/null\" \
     | sed -n 's/^-- cursor: //p' > /tmp/05-wake.cur"
# 60s display-off timeout (1-minute minimum) + grace; no input this period.
sleep 75
qdwin_screenshot /tmp/05-step2-asleep.png
```

**Assert (2.1):** screenshot is mostly black (avg brightness < 5).
This confirms DPMS off fired. If still bright, the idle/DPMS policy isn't
armed — diagnose `qs ipc call qdwin capabilities` (idleDpms) and the
qdshell `power.displayOff*` settings, and the user journal for
`idle policy armed: ... displayOff=60000ms`.

### Step 3 — wake screen, capture wake transition

```bash
qdwin_mouse_move 800 400
sleep 0.5
qdwin_mouse_move 850 450
sleep 2
qdwin_screenshot /tmp/05-step3-wake.png
```

**Assert (3.1):** bar visible in top 31 px again.
**Assert (3.2):** do not gate this scenario on OCR-reading the clock.
Record the top-center crop before and after wake if useful, but pass this
assert when the bar is visibly present after wake and Step 4's compositor
log is clean. OCR can miss the small clock text, and unchanged minute text
is possible when the wait/wake lands within the same displayed minute.

### Step 4 — confirm clean journal

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'cur=$(cat /tmp/05-wake.cur 2>/dev/null)
[ -n "$cur" ] || { echo "FAIL: missing pre-idle/wake compositor cursor"; exit 1; }
runuser -l admin -c "journalctl --user -u qdwin-compositor.service --after-cursor \"$cur\" --no-pager"' \
 > "${QCI_SCENARIO_TMPDIR:-/tmp}/05-weston.log"
```

**Assert (4.1):** zero `error <N>:` lines in the captured log.
**Assert (4.2):** the wake screenshot from Step 3 is the load-bearing
proof that the bar repainted. Treat `qdwin: layer-shell mapped` in the
captured log as diagnostic only: surfaces may remain mapped across DPMS
off/on, so absence of a fresh remap line is not a failure when the bar is
visibly present after wake and the compositor log is clean.

## Cleanup

```bash
# Restore default (DPMS disabled) so other scenarios don't suffer.
"$QDWIN_VM_EXEC" "$VMNAME" "
 SETTINGS=/home/\$(getent passwd 1000 | cut -d: -f1)/.config/qdshell/settings.json
 if [ -f \$SETTINGS ]; then
 python3 - <<PY
import json
p='\$SETTINGS'
d=json.load(open(p))
pw=d.setdefault('power',{})
pw['displayOffAC']=0
pw['displayOffBattery']=0
json.dump(d, open(p,'w'), indent=2)
PY
 fi
"
noct_restart
```

## Pass criteria

All asserts in steps 1-4 pass.

## Known failure modes

1. **Bar gone after wake** — would mean qdwin's view-mapping path
 doesn't re-map layer surfaces on output re-enable. 
 bug; would need a fix in `qdwin_layer_surface_apply()`.

2. **Wake produces black screen for >5s before bar appears** —
 acceptable; Noctalia's QML scenegraph takes a moment to repaint
 after frame-callback resumes. Tolerance window: 5 seconds.

3. **DPMS never fires (screenshot still bright at step 2)** — the idle
 path is owned by qdshell, NOT weston.ini. Verify (a) the capability
 gate: `qs ipc call qdwin capabilities` must report `idleDpms=true`
 (needs a v26+ bind + ext-idle-notify); (b) the settings were written
 to `~/.config/qdshell/settings.json` under `power.displayOff{AC,
 Battery}` in MINUTES (not the retired `idle.screenOffTimeout`, and not
 `~/.config/noctalia/`); (c) the user journal shows
 `idle policy armed: ... displayOff=60000ms` after `noct_restart`.
