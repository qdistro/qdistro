# 09 — lock-surface live-capture / egress indicators (J28)

**Acceptance criterion (security):** while the machine is locked, every
active-capture indicator matches reality — a live microphone, camera,
screencast or system-audio capture is visible on the lock surface, capture
that starts or stops *while locked* is reflected, an observer that dies or
hangs shows as a failure rather than as a quiet machine, and active silo
network egress (including a silo in transient `Stopping`) is shown.

This is the live gate for `todo/fable-release/06-human-test-plan.md` H5 and
exit criterion 11 in `07-release-checklist.md`. The unit suite
(`tests/unit/test_indicators.py`) pins the derivation against synthetic
`pw-dump` payloads; it cannot prove that the **installed** module sees the
**real** graph, that the banner is painted, or that the observer survives a
lock-state restart. That is what this scenario is for.

Two channels are asserted at every step, and both must agree:

1. **Pixels** — the banner border is exactly `#FD4663` (`Color.mError`)
   when capture is observed or the observer has failed, and is not present
   when the observer is healthy and nothing is observed. Do not replace
   this with a "looks fine" visual check.
2. **The installed module** — an in-guest `python3 -c` that imports
   `qdlocker.indicators` *from the installed package* (never from a
   checkout) and derives state from a live `pw-dump` / `ListSilos`. This
   simultaneously proves reachability: if the image ships a wheel without
   `indicators.py`, or without `pw-dump`, this fails loudly instead of
   silently rendering "unverified" forever.

Steps 4 (camera) and 8 (second output) are **conditional**: they SKIP with
an explicit reason when the VM has no camera device or cannot add an
output. A SKIP is reported, never silently passed.

## Setup

```bash
source "$(dirname "$0")/qdlocker-helpers.sh"
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdlocker_session_healthy || { echo "FAIL: session not up"; exit 2; }
qdlocker_drain_lock_state

qdwin_screenshot /tmp/qdlocker-09-step0-baseline.png
read -r SW SH < <(qdlocker_screenshot_dimensions /tmp/qdlocker-09-step0-baseline.png)

# The banner is anchored to the top of the lock surface. Assert inside a
# generous top band rather than at exact coordinates so a font/metric change
# does not turn into a false FAIL.
BANNER_H=$(( SH / 4 > 220 ? SH / 4 : 220 ))
BANNER_CROP="${SW}x${BANNER_H}+0+0"
ERR='#FD4663'   # shim/Color.qml mError — the banner border while alarming

# Helper: derive indicator state inside the guest from the INSTALLED module.
# `cd /` so a stray checkout in admin's home can never satisfy the import.
qdlocker_indicator_state() {
    local script b64
    script=$(cat <<'PY'
cd / && runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 python3 - <<'EOF'
import json, subprocess, sys
from qdlocker import indicators as I
print("MODULE:", I.__file__, file=sys.stderr)
tools = I.tools_available()
cap = subprocess.run(I.CAPTURE_CMD, capture_output=True, text=True)
egr = subprocess.run(I.EGRESS_CMD, capture_output=True, text=True)
ok, nodes = I.parse_pw_dump(cap.stdout if cap.returncode == 0 else "")
eok, rows = I.parse_list_silos(egr.stdout if egr.returncode == 0 else "")
state = {
    "tools": tools,
    "capture_exit": cap.returncode,
    "egress_exit": egr.returncode,
    "capture": I.summarise_capture(ok, nodes, fresh=True),
    "egress": I.summarise_egress(eok, rows, fresh=True),
}
print(json.dumps(state))
EOF
PY
)
    b64=$(printf '%s' "$script" | base64 -w0)
    "$QDWIN_VM_EXEC" "$VMNAME" "echo $b64 | base64 -d | bash"
}
```

## Steps

### Preflight A — VM graphics backend is compositing

Identical in purpose to `07-lock-occludes-desktop.md`: a VM whose DRM
atomic commits are all rejected produces black screenshots, and every pixel
assertion below would be a false FAIL. That is an environment ERROR, not a
product defect.

```bash
DRM_LOG=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "journalctl --user -u qdwin-compositor.service --boot --no-pager"' \
  | grep -E "atomic: couldn't commit new state: Invalid argument|repaint-flush failed: Invalid argument" || true)
DRM_FAILS=$(printf '%s\n' "$DRM_LOG" | grep -cE "atomic: couldn't commit new state|repaint-flush failed" || true)
printf '%s\n' "${DRM_FAILS:-0}" > "${QCI_SCENARIO_TMPDIR:-/tmp}/09-drm-baseline-count"
if [ "${DRM_FAILS:-0}" -ge 5 ]; then
    echo "ERROR: VM graphics backend is failing DRM atomic commits ($DRM_FAILS)." \
         "Screenshots would be black; this is not a J28 defect." >&2
    printf '%s\n' "$DRM_LOG" | tail -20 >&2
    exit 78
fi
```

**Preflight gate:** fewer than 5 repeated atomic/repaint failures.

### Preflight B — the observer is actually installed (reachability)

```bash
STATE=$(qdlocker_indicator_state)
printf '%s\n' "$STATE" | tee /tmp/qdlocker-09-preflight-state.json
python3 - "$STATE" <<'EOF'
import json, sys
s = json.loads(sys.argv[1])
assert s["tools"]["pw-dump"], "pw-dump is not installed in the image"
assert s["tools"]["busctl"], "busctl is not installed in the image"
assert s["capture_exit"] == 0, f"pw-dump exited {s['capture_exit']}"
EOF
```

**Assert (B.1):** the guest imports `qdlocker.indicators` from a path under
the installed prefix (the `MODULE:` line on stderr must NOT be under
`/home` or any checkout). A checkout-only import is the exact
reachability failure class recorded in
`todo/fable-release/10-reachability-audit-2026-07-26.md`.
**Assert (B.2):** `pw-dump` and `busctl` exist and `pw-dump` exits 0. If
either is missing, the shipped indicator can never be anything but
"unverified" — that is a **FAIL of the image**, not a pass of the code.

### Step 1 — locked with nothing capturing: no alarm, no false all-clear

```bash
qdlocker_ctrl lock
qdlocker_wait_for_lock 5
sleep 1.5   # > one 3s poll would be flaky; the lock edge forces an immediate scan
qdwin_screenshot /tmp/qdlocker-09-step1-quiet.png
qdlocker_assert_color_absent_in_crop \
  /tmp/qdlocker-09-step1-quiet.png "$ERR" "$BANNER_CROP" banner-quiet
STATE=$(qdlocker_indicator_state)
python3 - "$STATE" <<'EOF'
import json, sys
s = json.loads(sys.argv[1])["capture"]
assert s["observerOk"] is True, "observer should be healthy on a quiet machine"
assert s["anyActive"] is False, f"unexpected capture observed: {s['activeDetail']}"
# The coverage disclosure must still be present — a quiet scan is not an
# all-clear, and no kind may report "clear".
assert s["anyUnverified"] is True
assert "clear" not in {k["state"] for k in s["kinds"].values()}
EOF
```

**Assert (1.1):** no `#FD4663` in the banner band — a healthy quiet scan
must not look like an alarm.
**Assert (1.2):** the banner still shows the dim "capture monitoring:
partial" disclosure (visible in the screenshot; it is the only text in the
banner at this point).
**Assert (1.3):** no kind reports `clear`.

### Step 2 — a real microphone capture starts WHILE LOCKED

```bash
"$QDWIN_VM_EXEC" "$VMNAME" '
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
    setsid pw-record --target=@DEFAULT_SOURCE@ /tmp/qdlocker-09-mic.wav \
    >/tmp/qdlocker-09-mic.log 2>&1 &
'
sleep 5   # > one 3s poll: this deliberately does NOT re-lock, so only the
          # poll (not the lock edge) can pick the new capture up
qdwin_screenshot /tmp/qdlocker-09-step2-mic.png
qdlocker_assert_color_present_in_crop \
  /tmp/qdlocker-09-step2-mic.png "$ERR" "$BANNER_CROP" banner-mic-alarm
STATE=$(qdlocker_indicator_state)
python3 - "$STATE" <<'EOF'
import json, sys
s = json.loads(sys.argv[1])["capture"]
assert s["anyActive"] is True, "a live pw-record must be observed"
assert "microphone" in s["activeKinds"], s["activeKinds"]
assert s["kinds"]["microphone"]["state"] == "active"
EOF
```

**Assert (2.1):** the banner turns alarming (`#FD4663` present) without any
lock/unlock cycle — i.e. the poll observed a capture that began while
locked. This is the core J28 property.
**Assert (2.2):** the derived state names `microphone`, and the banner text
names the capturing client (`mic:pw-record`) or, if the graph attributes no
client, reads `(device active, client unknown)` — the two are different
claims and the surface must not conflate them.

### Step 3 — the capture stops while locked

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x pw-record || true'
sleep 5
qdwin_screenshot /tmp/qdlocker-09-step3-mic-stopped.png
qdlocker_assert_color_absent_in_crop \
  /tmp/qdlocker-09-step3-mic-stopped.png "$ERR" "$BANNER_CROP" banner-after-stop
```

**Assert (3.1):** the alarm clears within two poll intervals. A stuck-on
indicator is as much a failure as a stuck-off one: it trains the owner to
ignore it.

### Step 4 — camera (CONDITIONAL)

```bash
HAS_CAM=$("$QDWIN_VM_EXEC" "$VMNAME" 'ls /dev/video* 2>/dev/null | head -1' | tr -d '\r\n')
if [ -z "$HAS_CAM" ]; then
    echo "SKIP (4): no /dev/video* in this VM; camera capture not exercised." \
         "Re-run on a VM with v4l2loopback or a passed-through camera." >&2
else
    "$QDWIN_VM_EXEC" "$VMNAME" "
      runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        setsid pw-cat --record --target=\$(pw-cli ls Node | grep -i -m1 camera) \
        /tmp/qdlocker-09-cam.raw >/tmp/qdlocker-09-cam.log 2>&1 &
    "
    sleep 5
    qdwin_screenshot /tmp/qdlocker-09-step4-camera.png
    qdlocker_assert_color_present_in_crop \
      /tmp/qdlocker-09-step4-camera.png "$ERR" "$BANNER_CROP" banner-camera-alarm
    STATE=$(qdlocker_indicator_state)
    python3 - "$STATE" <<'EOF'
import json, sys
s = json.loads(sys.argv[1])["capture"]
assert "camera" in s["activeKinds"], s["activeKinds"]
EOF
    "$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x pw-cat || true'
fi
```

**Assert (4.1):** a camera stream is classified as `camera`, not as
`screencast`. If it lands in `screencast`, the classifier's camera hints
(`media.role`, `device.api`, name matching) need the VM's real property
shape added to them — capture the offending node's props into the report.

### Step 5 — screencast via qdwin's view-stream path

```bash
# qdwin pins a forwarded toplevel onto a weston backend-pipewire output and
# publishes it as `weston.pipewire-N` — the only screencast signal the shell
# can observe. Drive it exactly as the multimachine harness does.
"$QDWIN_VM_EXEC" "$VMNAME" '
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
    setsid qdistro-test-window --title qdlocker-09-cast --width 640 --height 400 \
    >/tmp/qdlocker-09-cast.log 2>&1 &
'
sleep 2
# Subscribe the toplevel to a view stream (see multimachine/harness/vm_backend.py
# `subscribe_view_stream`); the runner should use the same helper it uses for
# the mm scenarios rather than hand-rolling a Wayland client here.
echo "RUNNER: start a qdwin view stream for the 'qdlocker-09-cast' toplevel" >&2
sleep 5
qdwin_screenshot /tmp/qdlocker-09-step5-screencast.png
STATE=$(qdlocker_indicator_state)
python3 - "$STATE" <<'EOF'
import json, sys
s = json.loads(sys.argv[1])["capture"]
assert "screencast" in s["activeKinds"], (
    "a live weston.pipewire-N node must be classified as screencast: "
    f"{s['activeKinds']} / {s['activeDetail']}")
EOF
```

**Assert (5.1):** the `weston.pipewire-N` node is observed as `screencast`
while the stream is live, and the banner alarms.
**Known limitation to record in the report, not to assert:** a direct
`weston_capture_v1` grab (qdshell's own screenshot path) is invisible to
this observer by design — see `doc/sessions.md`. Do not add an assertion
that pretends otherwise.

### Step 6 — observer failure must be visible

```bash
# Make pw-dump unusable for the locker without touching the rest of the
# system: shadow it with a failing wrapper on the service's PATH.
"$QDWIN_VM_EXEC" "$VMNAME" '
  install -d -m 0755 /home/admin/.config/systemd/user/qdlocker.service.d
  cat >/home/admin/.config/systemd/user/qdlocker.service.d/91-break-pwdump.conf <<EOF
[Service]
Environment=PATH=/tmp/qdlocker-09-brokenbin:/usr/local/bin:/usr/bin:/bin
EOF
  install -d -m 0755 /tmp/qdlocker-09-brokenbin
  printf "#!/bin/sh\nsleep 30\n" > /tmp/qdlocker-09-brokenbin/pw-dump
  chmod 0755 /tmp/qdlocker-09-brokenbin/pw-dump
  chown -R admin:users /home/admin/.config/systemd/user/qdlocker.service.d
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service
'
sleep 4
qdlocker_ctrl lock
qdlocker_wait_for_lock 5
sleep 8   # past the 2.5s scan timeout and the 12s stale horizon
qdwin_screenshot /tmp/qdlocker-09-step6-observer-dead.png
qdlocker_assert_color_present_in_crop \
  /tmp/qdlocker-09-step6-observer-dead.png "$ERR" "$BANNER_CROP" banner-observer-dead
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "journalctl --user -u qdlocker.service --boot --no-pager"' \
  | grep -E "observer timed out; killing scan"
```

**Assert (6.1):** a hung `pw-dump` produces an *alarming* banner (observer
failed), NOT the dim quiet state. A machine nobody is watching must not
look like a machine where nothing is happening.
**Assert (6.2):** the journal shows the scan being killed — i.e. the hard
timeout fired rather than the scan hanging forever.
**Assert (6.3):** after removing the shim and restarting, the banner
returns to the quiet state (proves the failure is not sticky):

```bash
"$QDWIN_VM_EXEC" "$VMNAME" '
  rm -f /home/admin/.config/systemd/user/qdlocker.service.d/91-break-pwdump.conf
  rm -rf /tmp/qdlocker-09-brokenbin
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service
'
sleep 4
qdlocker_drain_lock_state
```

### Step 7 — silo egress, including transient `Stopping`

```bash
SILO=${QDLOCKER_09_SILO:-work}
"$QDWIN_VM_EXEC" "$VMNAME" "
  busctl --system call org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
    org.qdistro.SessionManager1 SetSiloEgress ss '$SILO' 'direct'
  busctl --system call org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
    org.qdistro.SessionManager1 StartSilo s '$SILO'
"
sleep 2
qdlocker_ctrl lock
qdlocker_wait_for_lock 5
sleep 4
STATE=$(qdlocker_indicator_state)
python3 - "$STATE" "$SILO" <<'EOF'
import json, sys
s = json.loads(sys.argv[1])["egress"]
assert s["active"] is True and sys.argv[2] in s["detail"], s
assert s["unverified"] is False
EOF

# Now stop it with a long grace window and sample DURING `Stopping`.
"$QDWIN_VM_EXEC" "$VMNAME" "
  busctl --system call org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
    org.qdistro.SessionManager1 StopSilo si '$SILO' 30 &
"
sleep 2
STATE=$(qdlocker_indicator_state)
python3 - "$STATE" "$SILO" <<'EOF'
import json, sys
s = json.loads(sys.argv[1])["egress"]
assert s["active"] is True, (
    "a silo in transient Stopping can still have live processes and a live "
    f"network path; hiding it is the fail-silent case: {s}")
EOF
```

**Assert (7.1):** an `Active` silo with `direct` egress is shown.
**Assert (7.2):** the same silo is STILL shown while `Stopping` — the
session manager emits that state before SIGTERM, the grace wait, SIGKILL
and egress teardown.
**Assert (7.3):** with the session manager stopped (`systemctl stop
qdistro-session-manager`), the egress row reads *unverified*, never "no
egress". Run this last; restart the unit afterwards.

### Step 8 — locked-state restart of qdlocker

```bash
qdlocker_ctrl lock
qdlocker_wait_for_lock 5
"$QDWIN_VM_EXEC" "$VMNAME" '
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
    systemctl --user restart qdlocker.service
'
sleep 5
qdlocker_ctrl status | grep 'locked=True'
qdwin_screenshot /tmp/qdlocker-09-step8-restarted.png
```

**Assert (8.1):** after a restart while locked, qdlocker comes back locked
(`initially_locked` at bind time) AND the banner is present again. The
observer is driven by the lock-edge signal, so a restart that came up
locked without re-scanning would leave the indicator permanently blank —
that is the specific regression this step exists to catch.

### Step 9 — second output (CONDITIONAL; documents a KNOWN GAP)

Needs a VM booted with two virtual heads (e.g. `virtio-gpu,max_outputs=2`
plus a second `<video>`/head in the domain XML). There is no helper for
adding one at runtime, and this scenario deliberately does not invent one:
if the domain has a single head, the step SKIPs.

```bash
if ! virsh -c qemu:///session screenshot "$VMNAME" \
        /tmp/qdlocker-09-step9-secondary.ppm --screen 1 >/dev/null 2>&1; then
    echo "SKIP (9): domain $VMNAME has no second head; re-run on a" \
         "two-head VM to exercise multi-output lock behaviour." >&2
else
    qdlocker_drain_lock_state
    qdlocker_ctrl lock
    qdlocker_wait_for_lock 5
    sleep 3
    virsh -c qemu:///session screenshot "$VMNAME" \
        /tmp/qdlocker-09-step9-secondary.ppm --screen 1
    virsh -c qemu:///session screenshot "$VMNAME" \
        /tmp/qdlocker-09-step9-primary.ppm --screen 0
    # Secondary must be uniformly black: qdwin's lock curtain spans the union
    # bounding box of every output, and all non-lock layers are unset
    # globally, so nothing else is composited anywhere.
    python3 - /tmp/qdlocker-09-step9-secondary.ppm <<'EOF'
import subprocess, sys
out = subprocess.run(["convert", sys.argv[1], "-format", "%c", "histogram:info:-"],
                     capture_output=True, text=True).stdout
nonblack = [l for l in out.splitlines()
            if l.strip() and "#000000" not in l and "srgb(0,0,0)" not in l]
assert not nonblack, f"secondary output is not uniformly black: {nonblack[:5]}"
EOF
fi
```

**Assert (9.1) — current documented behaviour, NOT the desired one:** the
secondary output is uniformly black. qdwin's curtain spans the union
bounding box (`qdwin_install_lock_curtain`) and `qdwin_hide_non_lock_layers`
unsets whole layers, so no desktop pixel may appear on any output. If this
fails it is a qdwin lock-curtain leak and outranks every other finding here.
**Assert (9.2) — the known gap:** the indicator banner appears on the
PRIMARY output only, because qdlocker creates a single fullscreen window and
qdwin fullscreens it onto `qdwin_primary_output()`. Recorded in
`todo/fable-release/11-j28-multi-output-lock-indicators.md`. **When
per-output locker windows land, this assertion inverts** — the banner must
then be present on every head, and this step becomes its gate.
**Assert (9.3) — hotplug:** attaching an output *while locked* must not
leave an uncurtained region. `qdwin_on_output_changed` does not currently
call `qdwin_install_lock_curtain` (only output *removal* does, via
`qdwin_output_boundary_transition`), so this is expected to FAIL today.
Report it as the qdwin defect it is — it is not a J28 regression, and it
predates this work.

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" '
  pkill -u admin -x pw-record 2>/dev/null || true
  pkill -u admin -x pw-cat 2>/dev/null || true
  pkill -u admin -x qdistro-test-window 2>/dev/null || true
  rm -f /home/admin/.config/systemd/user/qdlocker.service.d/91-break-pwdump.conf
  rm -rf /tmp/qdlocker-09-brokenbin
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload
  systemctl start qdistro-session-manager.service 2>/dev/null || true
'
qdlocker_drain_lock_state
sleep 2
```

## Known-broken-if

- **Preflight B fails on `MODULE:`** pointing into `/home/...` or a git
  checkout — the image is not shipping `qdlocker/indicators.py` in the
  wheel and the whole feature is unreachable in production. This is the
  reachability class from `10-reachability-audit-2026-07-26.md`; treat it
  as a release blocker, not a test-environment problem.
- **Preflight B fails on `pw-dump`** — the image lacks the PipeWire tools
  package. The indicator would render "unverified" forever, which is
  honest but useless. Fix the image, then re-run.
- **Step 2 passes but Step 3 never clears** — the poll or the freshness
  horizon is not running; check for a wedged scan in the journal.
- **Step 2 shows `(device active, client unknown)` for a plain
  `pw-record`** — the client-attributed path did not match, and the
  classifier is falling back to the `Audio/Source` device node. Not a
  security failure, but record the node props: the graph shape on this VM
  differs from the fixtures.
- **Step 6 shows the dim quiet banner instead of an alarm** — the
  fail-visible property is broken. This is the most serious failure this
  scenario can report: it means an unobserved machine looks safe.
- **Step 7 hides the `Stopping` silo** — the egress row is fail-silent
  during teardown.
- **Step 9.1 shows desktop pixels on the secondary output** — that is a
  qdwin lock-curtain leak and outranks everything else here.
