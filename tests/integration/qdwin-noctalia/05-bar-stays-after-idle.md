# 05 — bar still visible after DPMS wake

**Acceptance criterion:** after weston DPMSes the screen on idle
(>5min default) and the test wakes it via mouse motion, the
Noctalia bar reappears at its original position with no protocol
errors during the wake transition.

This exercises:
- Configure/ack handling on output power-cycle
- weston's DPMS-on path through layer-shell surfaces
- Idle-notify-v1 + idle-inhibit-v1 interaction on Noctalia's bar

This is a regression-detection test — the configure/ack code paths
in qdwin (+1.2) need to handle re-mapping after the
output cycles, which is a different code path from initial mapping.

## Setup

```bash
source qdwin/tests/gui/qdwin-helpers.sh
source tests/integration/qdwin-noctalia/noctalia-helpers.sh
qdwin_set_vm "${VMNAME:-noctalia-vis-260503-1021}"
noct_session_healthy || { echo "FAIL: noctalia not healthy"; exit 1; }

# Speed up idle-off for testing. Noctalia drives DPMS via its own
# `idle.screenOffTimeout` setting (Noctalia subscribes to
# ext-idle-notify-v1 with that value, runs `idle.screenOffCommand`
# on fire, and emits a wake signal on activity). Editing
# `weston.ini [core] idle-time` was the v1 approach but is silently
# ignored because Noctalia owns the idle path, not weston-desktop.
# Drop the Noctalia value from its default 600s to 10s so the test
# completes in ~15s. Also wire `screenOffCommand` to a no-op DRM
# blackout via `wlopm` (or just rely on the QML scenegraph's
# stop-paint behaviour if wlopm is absent — see Known failure modes).
ADMIN_USER=$("$QDWIN_VM_EXEC" "$VMNAME" 'getent passwd 1000 | cut -d: -f1' 2>/dev/null | tail -1)
SETTINGS=/home/$ADMIN_USER/.config/noctalia/settings.json
# Create-or-edit: a fresh VM may have no settings.json yet, in which case the
# old `if [ -f ]` gate silently skipped the timeout change and DPMS never
# fired. mkdir -p + load-or-default so the idle timeout is always applied.
"$QDWIN_VM_EXEC" "$VMNAME" "
 mkdir -p /home/$ADMIN_USER/.config/noctalia
 python3 - <<PY
import json, os
p='$SETTINGS'
try:
    d=json.load(open(p))
except (FileNotFoundError, ValueError):
    d={}
d.setdefault('idle',{})['screenOffTimeout']=10
d['idle'].setdefault('screenOffCommand','')
json.dump(d, open(p,'w'), indent=2)
PY
 chown -R $ADMIN_USER:$ADMIN_USER /home/$ADMIN_USER/.config/noctalia
"
noct_restart
```

## Steps

### Step 1 — capture awake baseline

```bash
noct_screenshot_awake /tmp/05-step1-awake.png
```

**Assert (1.1):** bar visible in top 31 px (same check as 01.1.2).

### Step 2 — wait for DPMS-off

```bash
# 8s idle + 2s grace; no input during this period
sleep 12
qdwin_screenshot /tmp/05-step2-asleep.png
```

**Assert (2.1):** screenshot is mostly black (avg brightness < 5).
This confirms DPMS off fired. If still bright, idle-time isn't
being honored — diagnose `weston.ini` and journal.

### Step 3 — wake screen, capture wake transition

```bash
qdwin_mouse_move 800 400
sleep 0.5
qdwin_mouse_move 850 450
sleep 2
qdwin_screenshot /tmp/05-step3-wake.png
```

**Assert (3.1):** bar visible in top 31 px again.
**Assert (3.2):** the bar's clock widget shows the current time
(not stale from step 1 — Noctalia should have ticked during the
DPMS-off period).

### Step 4 — confirm clean journal

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
 "runuser -l admin -c \"journalctl --user -u qdwin-compositor.service --since '1 minute ago' --no-pager\"" \
 > /tmp/05-weston.log
```

**Assert (4.1):** zero `error <N>:` lines in the captured log.
**Assert (4.2):** at least one `qdwin: layer-shell mapped` line
between step 2 and step 3 timestamps (proving Noctalia repainted
on wake).

## Cleanup

```bash
# Restore default idle so other scenarios don't suffer
"$QDWIN_VM_EXEC" "$VMNAME" "
 SETTINGS=/home/\$(getent passwd 1000 | cut -d: -f1)/.config/noctalia/settings.json
 if [ -f \$SETTINGS ]; then
 python3 - <<PY
import json
p='\$SETTINGS'
d=json.load(open(p))
d.get('idle',{})['screenOffTimeout']=600
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

3. **idle-time setting ignored** — older weston versions parse it
 under `[idle]` section, not `[core]`. Check `man weston.ini`
 for the current weston's expected location.
