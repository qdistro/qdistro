# 09 — lock-surface live-capture / egress indicators (J28)

**Acceptance criterion (security):** while the machine is locked, every
active-capture indicator matches reality — a live microphone, camera,
system-audio or screencast capture is visible on the lock surface, capture
that starts or stops *while locked* is reflected, an observer that dies or
hangs shows as a **failure** rather than as a quiet machine, and active silo
network egress (including a silo in transient `Stopping`) is shown.

This is the live gate for `todo/fable-release/06-human-test-plan.md` H5 and
exit criterion 11 in `07-release-checklist.md`. The unit suite
(`tests/unit/test_indicators.py`) pins the derivation against synthetic
`pw-dump` payloads; it cannot prove that the **installed** module sees a
**real** graph, that the banner is painted, or that the running observer's
lock-edge / timeout / freshness lifecycle behaves. That is what this is for.

Two channels are used. The ctrl-socket channel is machine-checked at every
step; banner pixels are machine-checked at the steps where the *rendering* is
the property under test (1, 2, 3, 4, 7, 8.3) and merely recorded elsewhere —
each step says which:

1. **The running observer** — `qdlocker_ctrl indicators` returns a
   space-separated `key=value` line snapshotted from the live
   `LockIndicators` object inside qdlocker (introspection-gated; the GUI lane
   enables it via `qdlocker_session_healthy`). This is the authoritative
   channel: it reflects the actual service lifecycle, not a re-derivation.
2. **Pixels** — the banner border is exactly `#FD4663` (`Color.mError`) when
   capture is observed *or the observer has failed*, and absent when the
   observer is healthy and nothing is observed. Per project convention the
   ctrl-socket assertion is authoritative and the screenshot backs it up.

Conditional steps SKIP with an explicit printed reason and are reported as
SKIP — never silently passed. Steps 4 (system audio), 5 (camera), 6
(screencast) and 10 (second output) are conditional; 1, 2, 3, 7, 8 and 9 are
not. Step 6 needs a **manually** driven view stream: the scenario does not
reimplement a Wayland client, and its correlation check turns "no new node"
into a SKIP rather than a pass.

## Setup

```bash
source "$(dirname "$0")/qdlocker-helpers.sh"
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdlocker_session_healthy || { echo "FAIL: session not up"; exit 2; }
qdlocker_drain_lock_state

# The qdwin golden intentionally has no work silos: adding one globally changes
# unrelated shell UI baselines.  Own a dedicated, denied-egress fixture for this
# scenario and remove it in Cleanup.
SILO=${QDLOCKER_09_SILO:-qdlocker09}
SILO_UID=${QDLOCKER_09_SILO_UID:-3909}
"$QDWIN_VM_EXEC" "$VMNAME" "
  runuser -u admin -- busctl --system call \
    org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
    org.qdistro.SessionManager1 CreateSilo si '$SILO' '$SILO_UID'
  runuser -u admin -- busctl --system call \
    org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
    org.qdistro.SessionManager1 SetSiloEgress ss '$SILO' none
"

qdwin_screenshot /tmp/qdlocker-09-step0-baseline.png
read -r SW SH < <(qdlocker_screenshot_dimensions /tmp/qdlocker-09-step0-baseline.png)

# The banner is anchored to the top of the lock surface. Assert inside a
# generous top band rather than at exact coordinates so a font/metric change
# is not a false FAIL.
BANNER_H=$(( SH / 4 > 220 ? SH / 4 : 220 ))
BANNER_CROP="${SW}x${BANNER_H}+0+0"
ERR='#FD4663'   # shim/Color.qml mError — the banner border while alarming

# Authoritative channel: one line from the RUNNING observer.
ind() { qdlocker_ctrl indicators | tr -d '\r'; }

# assert_ind <key> <expected>  — exact match on one key=value token.
assert_ind() {
    local key="$1" want="$2" line got
    line="$(ind)"
    got="$(printf '%s\n' "$line" | tr ' ' '\n' | grep "^${key}=" | cut -d= -f2-)"
    if [ "$got" != "$want" ]; then
        echo "FAIL: ${key}=${got:-<missing>} (want ${want})" >&2
        echo "  full line: $line" >&2
        return 1
    fi
    printf 'ok: %s=%s\n' "$key" "$got"
}

# assert_ind_contains <key> <substring>
assert_ind_contains() {
    local key="$1" want="$2" line got
    line="$(ind)"
    got="$(printf '%s\n' "$line" | tr ' ' '\n' | grep "^${key}=" | cut -d= -f2-)"
    case "$got" in
        *"$want"*) printf 'ok: %s=%s contains %s\n' "$key" "$got" "$want" ;;
        *) echo "FAIL: ${key}=${got:-<missing>} does not contain ${want}" >&2
           echo "  full line: $line" >&2; return 1 ;;
    esac
}
```

## Steps

### Preflight A — VM graphics backend is compositing

Same purpose as `07-lock-occludes-desktop.md`: a VM whose DRM atomic commits
are all rejected produces black screenshots and every pixel assertion below
would be a false FAIL. That is an environment ERROR, not a product defect.
(The ctrl-socket channel still works in that state, so if you hit this,
re-run with only the `assert_ind*` checks and report the pixel checks as
BLOCKED rather than passed.)

```bash
DRM_LOG=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "journalctl --user -u qdwin-compositor.service --boot --no-pager"' \
  | grep -E "atomic: couldn't commit new state: Invalid argument|repaint-flush failed: Invalid argument" || true)
DRM_FAILS=$(printf '%s\n' "$DRM_LOG" | grep -cE "atomic: couldn't commit new state|repaint-flush failed" || true)
if [ "${DRM_FAILS:-0}" -ge 5 ]; then
    echo "ERROR: VM graphics backend is failing DRM atomic commits ($DRM_FAILS)." >&2
    printf '%s\n' "$DRM_LOG" | tail -20 >&2
    exit 78
fi
```

### Preflight B — the observer is installed, from the installed prefix

```bash
# python3 -I: isolated mode ignores PYTHONPATH *and* user site-packages, and
# `cd /` removes the cwd. If the module still imports, it is the installed
# one. The path is ASSERTED, not merely printed.
MODPATH=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'cd / && runuser -u admin -- python3 -I -c "import qdlocker.indicators as m; print(m.__file__)"' \
  | tr -d '\r')
echo "installed module: $MODPATH"
case "$MODPATH" in
    /usr/lib*/python3*/site-packages/qdlocker/indicators.py|/usr/lib/qdistro/*|/usr/local/lib*/python3*/*/qdlocker/indicators.py)
        echo "ok: module resolves under an installed prefix" ;;
    "") echo "FAIL: qdlocker.indicators does not import in isolated mode —" \
             "the wheel does not ship it" >&2; exit 1 ;;
    /home/*|*/.worktrees/*|*/qdistro/qdlocker/qdlocker/*)
        echo "FAIL: module resolves to a CHECKOUT ($MODPATH), not the installed" \
             "package — this is the reachability failure class in" \
             "todo/fable-release/10-reachability-audit-2026-07-26.md" >&2; exit 1 ;;
    *) echo "FAIL: unexpected module path $MODPATH" >&2; exit 1 ;;
esac

# Both observer tools must exist in the image, or the indicator can only ever
# read "unverified" — honest, but useless.
"$QDWIN_VM_EXEC" "$VMNAME" 'command -v pw-dump >/dev/null' \
  || { echo "FAIL: pw-dump missing from the image (pipewire-tools)" >&2; exit 1; }
"$QDWIN_VM_EXEC" "$VMNAME" 'command -v busctl >/dev/null' \
  || { echo "FAIL: busctl missing from the image" >&2; exit 1; }

# And the ctrl channel itself must be live, or every assertion below is vacuous.
ind | grep -q 'capture_observer=' \
  || { echo "FAIL: ctrl 'indicators' unavailable — introspection not enabled" >&2; exit 1; }
```

**Assert (B.1):** the module imports in isolated mode from an installed
prefix. A checkout-only import is a release blocker, not a test-env issue.
**Assert (B.2):** `pw-dump` and `busctl` exist.
**Assert (B.3):** the `indicators` verb answers — otherwise the whole gate is
unfalsifiable and must be reported BLOCKED.

### Step 1 — locked with nothing capturing: healthy, quiet, and NOT an all-clear

```bash
qdlocker_ctrl lock
qdlocker_wait_for_lock 5
sleep 2            # the lock edge forces an immediate scan; one poll is 3s
assert_ind capture_observer ok
assert_ind capture_active 0
assert_ind egress_observer ok
qdwin_screenshot /tmp/qdlocker-09-step1-quiet.png
qdlocker_assert_color_absent_in_crop \
  /tmp/qdlocker-09-step1-quiet.png "$ERR" "$BANNER_CROP" banner-quiet
# No kind may be missing from the unverified list while nothing is observed:
# "clear" is not a state this product can produce.
assert_ind capture_unverified \
  microphone,camera,screencast,systemAudio,virtualInput,unattributed
```

**Assert (1.1):** the observer is healthy and saw nothing.
**Assert (1.2):** no `#FD4663` in the banner band — a healthy quiet scan must
not look like an alarm.
**Assert (1.3):** every kind is still `unverified`; nothing reports `clear`.

### Step 2 — a real microphone capture starts WHILE LOCKED

```bash
"$QDWIN_VM_EXEC" "$VMNAME" '
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
    setsid pw-record --target=@DEFAULT_SOURCE@ /tmp/qdlocker-09-mic.wav \
    >/tmp/qdlocker-09-mic.log 2>&1 &
'
sleep 6   # two polls; deliberately NO lock cycle, so only the poll can see it
assert_ind capture_active 1
assert_ind_contains capture_kinds microphone
assert_ind_contains capture_detail mic
qdwin_screenshot /tmp/qdlocker-09-step2-mic.png
qdlocker_assert_color_present_in_crop \
  /tmp/qdlocker-09-step2-mic.png "$ERR" "$BANNER_CROP" banner-mic-alarm
ATTR=$(ind | tr ' ' '\n' | grep '^capture_attributed=' | cut -d= -f2)
echo "capture_attributed=$ATTR"
if [ "$ATTR" = "0" ]; then
    assert_ind_contains capture_detail client_unknown
else
    ind | grep -q 'client_unknown' && {
        echo "FAIL: capture_attributed=1 but the detail says client unknown" >&2
        exit 1; }
fi
```

**Assert (2.1):** the running observer reports an active microphone without
any lock/unlock cycle — the poll saw a capture that began while locked. This
is the core J28 property.
**Assert (2.2):** the banner turns alarming.
**Assert (2.3):** the banner wording must match `capture_attributed`. Read the
banner text off the screenshot: `1` requires `LIVE CAPTURE` **and** a client
name; `0` requires `CAPTURE ACTIVITY` **and** `client unknown`. Either value is
acceptable — the mismatch is not. `capture_detail` in the same line carries the
suffix, so the two can be compared without OCR: with `ATTR=0` the detail must
contain `client_unknown`, and with `ATTR=1` it must not.

### Step 3 — the capture stops while locked

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x pw-record || true'
sleep 7
assert_ind capture_active 0
assert_ind capture_observer ok
qdwin_screenshot /tmp/qdlocker-09-step3-mic-stopped.png
qdlocker_assert_color_absent_in_crop \
  /tmp/qdlocker-09-step3-mic-stopped.png "$ERR" "$BANNER_CROP" banner-after-stop
```

**Assert (3.1):** the alarm clears within two polls, on both channels. A
stuck-on indicator is as much a failure as a stuck-off one — it trains the
owner to ignore it.

### Step 4 — system-audio (sink-monitor) capture

```bash
MON=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     pactl get-default-sink' | tr -d '\r')
if [ -z "$MON" ]; then
    echo "SKIP (4): no default sink; system-audio capture not exercised." >&2
else
    "$QDWIN_VM_EXEC" "$VMNAME" "
      runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        setsid parec -d ${MON}.monitor -r /tmp/qdlocker-09-sysaudio.raw \
        >/tmp/qdlocker-09-sysaudio.log 2>&1 &
    "
    sleep 6
    assert_ind capture_active 1
    assert_ind_contains capture_kinds systemAudio
    # The discrimination is the point: a monitor capture must NOT also raise a
    # microphone alarm. "Your mic is live" and "your speakers are being
    # recorded" are different statements to the owner.
    if ind | tr ' ' '\n' | grep '^capture_kinds=' | grep -q microphone; then
        echo "FAIL: sink-monitor capture also reported as microphone" >&2
        ind >&2
        exit 1
    fi
    qdwin_screenshot /tmp/qdlocker-09-step4-sysaudio.png
    qdlocker_assert_color_present_in_crop \
      /tmp/qdlocker-09-step4-sysaudio.png "$ERR" "$BANNER_CROP" banner-sysaudio
    "$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x parec || true'
    sleep 7
    assert_ind capture_active 0
fi
```

**Assert (4.1):** a monitor capture is classified `systemAudio`, not
`microphone`. If it lands in `microphone`, the `stream.capture.sink`
discriminator did not fire on this stack — record the node props; that is a
real classification defect, since "the mic is live" and "your speakers are
being recorded" are different statements to the owner.

### Step 5 — camera (CONDITIONAL)

```bash
CAMID=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'cd / && runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 python3 -I -c "
import json,subprocess
from qdlocker import indicators as I
ok,nodes=I.parse_pw_dump(subprocess.run(I.CAPTURE_CMD,capture_output=True,text=True).stdout)
for n in nodes:
    p=n[\"props\"]
    if str(p.get(\"media.class\",\"\"))==\"Video/Source\":
        print(p.get(\"node.name\",\"\")); break
"' | tr -d '\r')
if [ -z "$CAMID" ]; then
    echo "SKIP (5): no PipeWire Video/Source node in this VM (no camera," \
         "no v4l2loopback). Camera classification not exercised." >&2
elif ! "$QDWIN_VM_EXEC" "$VMNAME" 'command -v gst-launch-1.0 >/dev/null'; then
    echo "SKIP (5): gst-launch-1.0 not installed; cannot drive the camera node." >&2
else
    "$QDWIN_VM_EXEC" "$VMNAME" "
      runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        setsid gst-launch-1.0 -q pipewiresrc path=\$(
          runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
            pw-cli ls Node | awk '/id /{id=\$2} /node.name = \"${CAMID}\"/{print id; exit}'
        ) ! fakesink >/tmp/qdlocker-09-cam.log 2>&1 &
    "
    sleep 6
    # The driver must actually be running, or a SKIP is masquerading as a pass.
    "$QDWIN_VM_EXEC" "$VMNAME" 'pgrep -u admin -x gst-launch-1.0 >/dev/null' \
      || { echo "SKIP (5): gst-launch-1.0 exited immediately; see" \
                "/tmp/qdlocker-09-cam.log in the guest (node id parse?)." >&2; \
           CAM_SKIPPED=1; }
    if [ -z "${CAM_SKIPPED:-}" ]; then
    assert_ind capture_active 1
    assert_ind_contains capture_kinds camera
    qdwin_screenshot /tmp/qdlocker-09-step5-camera.png
    qdlocker_assert_color_present_in_crop \
      /tmp/qdlocker-09-step5-camera.png "$ERR" "$BANNER_CROP" banner-camera
    fi
    "$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x gst-launch-1.0 || true'
    sleep 7
fi
```

**Assert (5.1):** a camera stream classifies as `camera`, not `screencast`.
If it lands in `screencast`, the camera hints (`media.role`, `device.api`,
name matching) need this VM's real property shape added to them — capture the
offending node's props into the report.

### Step 6 — screencast via qdwin's view-stream (CONDITIONAL, MANUAL DRIVER)

The only screencast signal the observer can see is the `weston.pipewire-N`
node qdwin publishes when it pins a forwarded toplevel onto a
`backend-pipewire` output. Driving that needs the multimachine harness's
view-stream subscription — this scenario does **not** reimplement a Wayland
client. Correlation is asserted: the node set is sampled before and after, so
a pre-existing node cannot make the step trivially green.

```bash
MM=/home/play2/qdistro/qdistro/multimachine/harness
NODES_BEFORE=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 pw-cli ls Node' \
  | grep -c 'weston\.pipewire' || true)
if [ ! -d "$MM" ]; then
    echo "SKIP (6): multimachine harness not present at $MM; screencast" \
         "classification not exercised." >&2
else
    # NOT AUTOMATED: this scenario does not reimplement a Wayland view-stream
    # client, and the mm harness's subscribe_view_stream is not a standalone
    # entry point for an arbitrary already-running toplevel — its source stack
    # performs the subscription as part of a larger setup. So this step is a
    # MANUAL driver: the runner starts a view stream by whatever means the mm
    # lane uses, and the correlation below decides PASS/SKIP. A step that
    # cannot create the node reports SKIP; it never reports PASS.
    echo "RUNNER (manual): start a qdwin view stream against $VMNAME now." >&2
    sleep 5
    NODES_AFTER=$("$QDWIN_VM_EXEC" "$VMNAME" \
      'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 pw-cli ls Node' \
      | grep -c 'weston\.pipewire' || true)
    if [ "$NODES_AFTER" -le "$NODES_BEFORE" ]; then
        echo "SKIP (6): no new weston.pipewire node appeared" \
             "($NODES_BEFORE -> $NODES_AFTER); the view stream did not start," \
             "so there is nothing to assert." >&2
    else
        sleep 4
        assert_ind_contains capture_kinds screencast
        qdwin_screenshot /tmp/qdlocker-09-step6-screencast.png
        qdlocker_assert_color_present_in_crop \
          /tmp/qdlocker-09-step6-screencast.png "$ERR" "$BANNER_CROP" banner-screencast
    fi
fi
```

**Assert (6.1):** a newly created `weston.pipewire-N` node is observed as
`screencast` while the stream is live.
**Not asserted, by design:** a direct `weston_capture_v1` grab is invisible to
this observer (`doc/sessions.md`). Do not add an assertion that pretends
otherwise.

### Step 7 — observer failure must be visible (the most important step)

```bash
"$QDWIN_VM_EXEC" "$VMNAME" '
  install -d -m 0755 /tmp/qdlocker-09-brokenbin
  printf "#!/bin/sh\nsleep 300\n" > /tmp/qdlocker-09-brokenbin/pw-dump
  chmod 0755 /tmp/qdlocker-09-brokenbin/pw-dump
  install -d -m 0755 /home/admin/.config/systemd/user/qdlocker.service.d
  cat >/home/admin/.config/systemd/user/qdlocker.service.d/91-break-pwdump.conf <<EOF
[Service]
Environment=PATH=/tmp/qdlocker-09-brokenbin:/usr/local/bin:/usr/bin:/bin
EOF
  chown -R admin:users /home/admin/.config/systemd/user/qdlocker.service.d
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service
'
sleep 4
qdlocker_enable_introspection    # the restart dropped the socket; re-arm it
qdlocker_ctrl lock
qdlocker_wait_for_lock 5
sleep 6            # past the 2.5s scan timeout; the kill publishes immediately
assert_ind capture_observer failed
assert_ind capture_active 0
qdwin_screenshot /tmp/qdlocker-09-step7-observer-dead.png
qdlocker_assert_color_present_in_crop \
  /tmp/qdlocker-09-step7-observer-dead.png "$ERR" "$BANNER_CROP" banner-observer-failed
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "journalctl --user -u qdlocker.service --boot --no-pager"' \
  | grep -E "observer timed out; killing scan" \
  || { echo "FAIL: the hard timeout did not fire" >&2; exit 1; }
```

**Assert (7.1):** `capture_observer=failed` — a hung `pw-dump` is reported as
a failed observer, not as a quiet machine.
**Assert (7.2):** the banner is ALARMING (error colour present). If the banner
is dim here, the fail-visible property is broken and this is the most serious
failure this scenario can report.
**Assert (7.3):** the journal shows the scan being killed — the hard timeout
fired rather than the scan hanging forever.

Recovery (also asserted, so a failure cannot be sticky):

```bash
"$QDWIN_VM_EXEC" "$VMNAME" '
  rm -f /home/admin/.config/systemd/user/qdlocker.service.d/91-break-pwdump.conf
  rm -rf /tmp/qdlocker-09-brokenbin
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service
'
sleep 4
qdlocker_enable_introspection
qdlocker_ctrl lock
qdlocker_wait_for_lock 5
sleep 3
assert_ind capture_observer ok
```

**Assert (7.4):** the observer recovers to `ok` after the tool is restored.

### Step 8 — silo egress, including transient `Stopping` and an unreachable manager

```bash
SILO=${QDLOCKER_09_SILO:-qdlocker09}
"$QDWIN_VM_EXEC" "$VMNAME" "
  runuser -u admin -- busctl --system call org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
    org.qdistro.SessionManager1 SetSiloEgress ss '$SILO' 'direct'
  runuser -u admin -- busctl --system call org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
    org.qdistro.SessionManager1 StartSilo s '$SILO'
"
sleep 4
assert_ind egress_active 1
assert_ind_contains egress_detail "$SILO"
qdwin_screenshot /tmp/qdlocker-09-step8-egress.png

# Sample DURING the transient Stopping state (30s grace window).
"$QDWIN_VM_EXEC" "$VMNAME" "
  setsid runuser -u admin -- busctl --system call \
    org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
    org.qdistro.SessionManager1 StopSilo si '$SILO' 30 >/dev/null 2>&1 &
"
sleep 4
# Correlate: the TARGET silo must be in Stopping in the SAME window in which
# the indicator still shows it. Another active silo, or a sample taken before
# the state transition, must not be able to satisfy this.
# Reuse the production parser (the installed module) rather than hand-rolling
# busctl string surgery in the harness.
"$QDWIN_VM_EXEC" "$VMNAME" "cd / && runuser -u admin -- python3 -I -c \"
import subprocess, sys
from qdlocker import indicators as I
out = subprocess.run(I.EGRESS_CMD, capture_output=True, text=True)
ok, rows = I.parse_list_silos(out.stdout)
assert ok, 'ListSilos unreadable'
row = [r for r in rows if r.get('name') == '$SILO']
assert row, 'silo $SILO absent from ListSilos'
state = row[0].get('state')
assert state == 'Stopping', (
    'silo $SILO is ' + str(state) + ', not Stopping — the sample missed the '
    'transient window. Re-run with a longer grace; this step cannot pass '
    'without observing Stopping.')
print('ok: silo $SILO is Stopping')
\"" || exit 1
assert_ind egress_active 1              # still live: Stopping is not dark
assert_ind_contains egress_detail "$SILO"

# Unreachable session manager must read UNVERIFIED, never "no egress".
"$QDWIN_VM_EXEC" "$VMNAME" 'systemctl stop qdistro-session-manager.service'
sleep 5
assert_ind egress_observer failed
assert_ind egress_active 0
# The UI claim is asserted too: the egress-unverified row is drawn in mError,
# so the banner band must contain the error colour even with capture quiet.
qdwin_screenshot /tmp/qdlocker-09-step8-egress-unverified.png
qdlocker_assert_color_present_in_crop \
  /tmp/qdlocker-09-step8-egress-unverified.png "$ERR" "$BANNER_CROP" banner-egress-unverified
"$QDWIN_VM_EXEC" "$VMNAME" 'systemctl start qdistro-session-manager.service'
sleep 4
assert_ind egress_observer ok
```

**Assert (8.1):** an `Active` silo with `direct` egress is shown.
**Assert (8.2):** the same silo is STILL shown while `Stopping` — the session
manager emits that state before SIGTERM, the grace wait, SIGKILL and egress
teardown, so the network path can still exist.
**Assert (8.3):** with the session manager stopped, egress reads
`egress_observer=failed`, and the banner shows the "network egress state
unverified" row — never a silent "no egress".
**Assert (8.4):** it recovers when the unit comes back.

### Step 9 — locked-state restart of qdlocker

```bash
qdlocker_drain_lock_state
qdlocker_ctrl lock
qdlocker_wait_for_lock 5
"$QDWIN_VM_EXEC" "$VMNAME" '
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
    systemctl --user restart qdlocker.service
'
sleep 5
qdlocker_enable_introspection
qdlocker_ctrl status | grep 'locked=True' \
  || { echo "FAIL: locker did not come back locked" >&2; exit 1; }
assert_ind capture_observer ok
qdwin_screenshot /tmp/qdlocker-09-step9-restarted.png
```

**Assert (9.1):** after a restart while locked, qdlocker comes back locked
(`initially_locked` at bind time) **and the observer has produced a fresh
reading** (`capture_observer=ok`). A locker that came up locked without
re-scanning would leave the indicator permanently blank — that specific
regression is what this step exists to catch. The `ready(initially_locked=1)`
path is the only thing that can drive it, so this is a genuine test of that
wiring, not of the poll.

### Step 10 — second output (CONDITIONAL; documents a KNOWN GAP)

Requires a VM booted with two enabled heads. A successful
`virsh screenshot --screen 1` alone is **not** proof — a domain can expose a
scanout the compositor never enabled — so the compositor's own output count
is checked first.

```bash
# qdwin's own enabled-output set, not DRM connector state: a connected
# connector the compositor never enabled would make the black-screen assertion
# below trivially green. The compositor logs an output_created per enabled
# output; count the distinct names still present.
OUTPUTS=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "journalctl --user -u qdwin-compositor.service --boot --no-pager"' \
  | grep -oE "output_created[^\n]*name=[A-Za-z0-9-]+" | grep -oE "name=[A-Za-z0-9-]+" \
  | sort -u | wc -l)
echo "qdwin enabled outputs: $OUTPUTS"
if [ "${OUTPUTS:-0}" -lt 2 ]; then
    echo "SKIP (10): compositor has ${OUTPUTS:-0} connected output(s); re-run" \
         "on a two-head VM to exercise multi-output lock behaviour." >&2
else
    qdlocker_drain_lock_state
    qdlocker_ctrl lock
    qdlocker_wait_for_lock 5
    sleep 3
    virsh -c qemu:///session screenshot "$VMNAME" \
        /tmp/qdlocker-09-step10-secondary.ppm --screen 1
    virsh -c qemu:///session screenshot "$VMNAME" \
        /tmp/qdlocker-09-step10-primary.ppm --screen 0
    # Primary must not be blank. NOTE: this is a weak check — it establishes
    # that the locker surface is being painted, NOT that the J28 banner is
    # present on it. Asserting the banner itself needs a text/pixel signature
    # the runner can match; until that exists, treat 10.2 as observed, not
    # gated.
    python3 - /tmp/qdlocker-09-step10-primary.ppm <<'EOF'
import subprocess, sys
out = subprocess.run(["convert", sys.argv[1], "-format", "%c",
                      "histogram:info:-"], capture_output=True, text=True).stdout
colors = [l for l in out.splitlines() if l.strip()]
assert len(colors) > 1, "primary output is uniformly one colour — no lock UI"
EOF
    # Secondary must be uniformly black: qdwin's curtain spans the union bbox
    # of the outputs present when it was installed, and all non-lock layers are
    # unset globally.
    python3 - /tmp/qdlocker-09-step10-secondary.ppm <<'EOF'
import subprocess, sys
out = subprocess.run(["convert", sys.argv[1], "-format", "%c",
                      "histogram:info:-"], capture_output=True, text=True).stdout
nonblack = [l for l in out.splitlines()
            if l.strip() and "#000000" not in l and "srgb(0,0,0)" not in l]
assert not nonblack, f"secondary output is not uniformly black: {nonblack[:5]}"
EOF
fi
```

**Assert (10.1):** the secondary output is uniformly black. No desktop pixel
may appear on any output. A failure here is a qdwin lock-curtain leak and
outranks every other finding in this scenario.
**Assert (10.2) — the known gap, asserted as current behaviour:** the banner
is on the PRIMARY output only, because qdlocker creates a single fullscreen
window and qdwin fullscreens it onto `qdwin_primary_output()`. See
`todo/fable-release/12-j28-multi-output-lock-indicators.md`. **When per-output
locker windows land, 10.2 inverts** — the banner must then be present on every
head, and this step becomes its gate.

**NOT AUTOMATED — output hotplug while locked.** `qdwin_on_output_changed`
does not re-install the lock curtain (only output *removal* and the
output-management apply path do), so an output attached while locked is
expected to fall outside the curtain. There is no scripted head-hotplug for
this VM setup, so this is a **manual** check: attach a head while locked and
observe. It is a pre-existing qdwin defect recorded in the decision note, not
a J28 regression — do not report it as a J28 failure.

## Cleanup

```bash
SILO=${QDLOCKER_09_SILO:-qdlocker09}
"$QDWIN_VM_EXEC" "$VMNAME" "
  pkill -u admin -x pw-record 2>/dev/null || true
  pkill -u admin -x parec 2>/dev/null || true
  pkill -u admin -x gst-launch-1.0 2>/dev/null || true
  pkill -u admin -x qdistro-test-window 2>/dev/null || true
  rm -f /home/admin/.config/systemd/user/qdlocker.service.d/91-break-pwdump.conf
  rm -rf /tmp/qdlocker-09-brokenbin
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload
  systemctl start qdistro-session-manager.service 2>/dev/null || true
  runuser -u admin -- busctl --system call \
    org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
    org.qdistro.SessionManager1 SetSiloEgress ss '$SILO' none 2>/dev/null || true
  runuser -u admin -- busctl --system call \
    org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
    org.qdistro.SessionManager1 StopSilo si '$SILO' 0 2>/dev/null || true
  for i in \$(seq 1 20); do
    runuser -u admin -- busctl --system call \
      org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
      org.qdistro.SessionManager1 DeleteSilo s '$SILO' >/dev/null 2>&1 && break
    sleep 1
  done
"
qdlocker_drain_lock_state
sleep 2
```

## Known-broken-if

- **Preflight B.1 resolves to a checkout** — the wheel does not ship
  `indicators.py` and the whole feature is unreachable in production. Release
  blocker (see `10-reachability-audit-2026-07-26.md`), not a test-env issue.
- **Preflight B.2 fails on `pw-dump`** — the image lacks `pipewire-tools`.
  The indicator would read "unverified" forever: honest, but useless.
- **Preflight B.3 fails** — introspection is not enabled, so every
  `assert_ind` would be vacuous. Report BLOCKED, never PASS.
- **Step 2 passes but Step 3 never clears** — the poll or the freshness
  horizon is wedged; check the journal for a stuck scan.
- **Step 4 fails its "not microphone" check** — the `stream.capture.sink`
  discriminator (or the `.monitor` name check on the device node) did not fire
  on this stack. Record the node props; "your mic is live" and "your speakers
  are being recorded" say different things to the owner.
- **Step 7 shows the dim banner instead of an alarm** — the fail-visible
  property is broken: an unobserved machine looks safe. Most serious failure
  available here.
- **Step 8 hides the `Stopping` silo** — the egress row is fail-silent during
  teardown.
- **Step 9 comes back locked but `capture_observer` is not `ok`** — the
  `ready(initially_locked)` path does not drive a rescan, so a restart while
  locked leaves the indicator blank.
- **Step 10.1 shows desktop pixels on the secondary output** — qdwin
  lock-curtain leak; outranks everything else here.
