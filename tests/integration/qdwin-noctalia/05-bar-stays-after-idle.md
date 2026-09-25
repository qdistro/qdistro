# 05 — bar still visible after DPMS wake

<!-- qci:visual: required -->

**Acceptance criterion:** after qdwin DPMSes the screen on idle
(armed here to the 1-minute minimum via the qdshell `power.displayOff*`
policy) and the test wakes it via mouse motion, the Noctalia bar
reappears at its original position with no protocol errors during the
wake transition.

A host that can commit DPMS-on stays on that path: the bar is visible in
the top 31 px after wake, and the compositor journal has zero error lines.
virtio-gpu may reject the DPMS-on atomic commit. That journal record is a
SKIP from step 3 (shell exit 0), not a failed bar assert. Step 4 does not
run after that SKIP.

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
# CAPTURE THROUGH A FILE, NEVER THROUGH `$( ... 2>&1 )`. That shape hands
# vm-exec's fd 1 AND fd 2 to the substitution's pipe. vm-exec redirects its own
# children's fd 1 to an internal capture file, but fd 2 is inherited straight
# through to every virsh/jq descendant it starts; one that outlives vm-exec and
# keeps that descriptor holds the pipe -- and this call -- open long after the
# guest command is dead, and an outer `timeout` cannot help because the shell is
# blocked on the read. A read from a regular file reaches EOF at the current end
# of file however many writers still hold it open.
#
# The capture is per call and UNLINKED before the command starts, so a survivor
# of the primary call cannot append into the fallback call's capture (which
# would corrupt both the fallback decision and the returned IPC response), and
# no capture is left NAMED while a command runs. Same shape as bounded_run() in
# qdistro/scripts/vm/vm-exec.
# The read is ceilinged in BYTES, with `head -c`. It was `read -N` until
# 2026-09-17, which ceilings CHARACTERS: bash drops NUL bytes and they do not
# count, so a NUL-bearing capture was read PAST the ceiling. The comment here
# also claimed that could not hang, "because a read of a regular file returns
# at the current EOF however many writers still hold it open" -- that is WRONG,
# EOF is re-tested on every read, and a 256 KiB nominal replay was measured at
# 9.57s against a writer staying ahead of it.
# This adds no size bound of its own: the only limit on the nameless inode is
# the RLIMIT_FSIZE vm-exec installs on its own children.
QD_IPC_CAP_BYTES=${QD_IPC_CAP_BYTES:-65536}
# Not a positive decimal integer => not a byte ceiling: `head -c -1` is
# "all but the last byte" and `head -c 00` reads nothing.
case "$QD_IPC_CAP_BYTES" in
    ''|*[!0-9]*|0|0*)
        echo "QD_IPC_CAP_BYTES must be a positive integer number of bytes, got '$QD_IPC_CAP_BYTES'" >&2
        exit 2 ;;
esac
qd_cap() {  # qd_cap <cmd...> -> runs <cmd> in the VM, prints its merged output
    local cf wfd rfd out="" rc=0 _qc_size=""
    cf=$(mktemp "${TMPDIR:-/tmp}/qd05-cap.XXXXXXXX") || return 125
    # Every setup step is CHECKED. Unchecked, a failing open or unlink let this
    # function run the command anyway and return its status, leaving the capture
    # NAMED for the whole run -- the opposite of what the unlink is for.
    # Reproduced by injecting a failing rm: it returned 0, printed COMMAND-RAN
    # and left the file (sol, qci-A-260917-sol-review.md section 3).
    exec {wfd}>"$cf" || { rm -f "$cf"; return 125; }
    exec {rfd}<"$cf" || { exec {wfd}>&-; rm -f "$cf"; return 125; }
    rm -f "$cf" || { exec {wfd}>&- {rfd}<&-; return 125; }
    "$QDWIN_VM_EXEC" "$VMNAME" "$@" >&"$wfd" 2>&"$wfd" {wfd}>&- {rfd}<&- || rc=$?
    exec {wfd}>&-
    # A failed replay is a capture failure, not empty output (sol A3 f3).
    if ! out=$(head -c "$QD_IPC_CAP_BYTES" <&"$rfd"); then
        echo "capture replay FAILED; output unavailable, not empty" >&2
        exec {rfd}<&-
        return 125
    fi
    # Overflow must not be silent: these captures carry CONTROL information
    # (the `no running instance|No such` PID fallback, state/geometry tokens),
    # and a marker past the cap reads exactly like a marker that never
    # appeared -- so the fallback is skipped and the step reports success on a
    # reply it never fully saw (sol, qci-A2-260917-sol-review.md section 2).
    # WHAT THIS ESTABLISHES, AND WHAT IT DOES NOT. Stated plainly because the
    # two previous attempts here both overclaimed.
    #
    # DETECTED: the capture holds more bytes than the replay returned -- a
    # stored suffix this function did not deliver. A real, local fact about
    # this file and this cap.
    #
    # NOT DETECTED: a writer that hit its own RLIMIT_FSIZE, handled EFBIG and
    # exited zero. The previous version compared the size against the PARENT's
    # `ulimit -f -H` and claimed to catch exactly that. It cannot, for two
    # independent reasons (astra, A6 findings 1 and 2):
    #   * `-H` is the HARD limit, but writes are constrained by the SOFT one,
    #     so `ulimit -S -f 1` under an unlimited hard limit went undetected.
    #   * The writer is not this process. vm-exec is a child, `bounded_run`
    #     sets its own limit for the inner RPC capture, and a descendant
    #     writing into the outer merged capture can have a different limit
    #     again -- one file, several limits, so no single parent-side number
    #     describes them.
    # And `>=` against that number turned a HEALTHY exact-fit write into a
    # fatal 125 that positively asserted the output "was cut off": a false red,
    # the very class of defect this workstream exists to remove.
    #
    # Proving completeness needs evidence from the WRITER (an explicit
    # completion marker), not a size. Until that exists this reports what it
    # can and stays silent about what it cannot. Equality at the cap is
    # ACCEPTED -- an exact fit is a legitimate write.
    if ! _qc_size=$(stat -Lc %s "/proc/self/fd/$rfd" 2>/dev/null); then
        echo "could not stat the capture; completeness is UNKNOWN, not verified" >&2
        exec {rfd}<&-
        return 125
    fi
    if [ "$_qc_size" -gt "$QD_IPC_CAP_BYTES" ]; then
        echo "capture (${_qc_size} bytes) exceeded $QD_IPC_CAP_BYTES bytes; reply INCOMPLETE" >&2
        exec {rfd}<&-
        return 125
    fi
    exec {rfd}<&-
    printf '%s' "$out"
    return "$rc"
}
qs_ipc() {
    # A CAPTURE failure (125 from qd_cap: setup, overflow or a failed
    # replay) is not a guest answer and must not be printed as one -- it
    # reads as 'the command ran and said nothing', which is how the
    # `no running instance` PID fallback gets silently skipped (sol,
    # qci-A3-260917-sol-review.md finding 2).
    local out pid _qc=0
    out=$(qd_cap \
        "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
         qs ipc -p $QS_PATH call qdwin $*") || _qc=$?
    if printf '%s' "$out" | grep -qiE 'no running instance|No such'; then
        # Plain `$( ... )` with NO host stderr redirect: vm-exec's fd 2 is
        # this script's stderr, not the substitution pipe, so a surviving
        # descendant cannot hold this call open -- vm-exec's own children get a
        # capture file on fd 1, so the pipe's only holder is vm-exec itself.
        pid=$("$QDWIN_VM_EXEC" "$VMNAME" \
            "pgrep -u admin -f '[q]s -p $QS_PATH' | while read p; do \
               grep -q dbus-run-session /proc/\$p/cmdline 2>/dev/null || { echo \$p; break; }; done")
        [ -n "$pid" ] || { printf '%s\n' "$out"; return 1; }
        out=$(qd_cap \
            "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
             qs ipc --pid $pid call qdwin $*") || _qc=$?
    fi
    if [ "$_qc" = 125 ]; then
        echo "qs_ipc: capture failed for '$*' (no usable reply)" >&2
        return 125
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
# DPMS verdict comes from sysfs, NOT a screenshot: while the output is
# DPMS-off the compositor suspends repaint, so the shell-capture path
# (qdwin_screenshot) cannot service a capture until wake — and the kernel
# DRM connector state is the ground truth for "display off" anyway.
"$QDWIN_VM_EXEC" "$VMNAME" 'cat /sys/class/drm/card0-Virtual-1/dpms' \
    > /tmp/05-step2-dpms.txt
```

**Assert (2.1):** `/tmp/05-step2-dpms.txt` reads exactly `Off`.
This confirms DPMS off fired. If it still reads `On`, the idle/DPMS policy
isn't armed — diagnose `qs ipc call qdwin capabilities` (idleDpms) and the
qdshell `power.displayOff*` settings, and the user journal for
`idle policy armed: ... displayOff=60000ms`.

### Step 3 — wake screen, capture wake transition

> **Driver note (MUST):** after the mouse moves, the shell below polls the
> cursor-scoped `qdwin-compositor.service` journal for about 5s. If that
> shell prints a line starting with `SKIP:`, write `SKIP <reason>` to
> status.txt and exit 0. Do not keep polling screenshots for the bar.
> Step 4 does not run after this SKIP (the atomic line would also trip the
> error-line assert, which is why the skip must happen before that assert
> and must end the scenario). A missing `/tmp/05-wake.cur` is FAIL, not
> SKIP. If the shell does not print `SKIP:`, fall through to the screenshot
> and the strict bar assert. Absence of the signature is not a skip.

```bash
qdwin_mouse_move 800 400
sleep 0.5
qdwin_mouse_move 850 450
# Wake check is the compositor journal after the step-2 cursor in
# /tmp/05-wake.cur (journalctl --user -u qdwin-compositor.service
# --after-cursor), not a black screenshot. The poll returns 0 only for a
# line that ends in "atomic: couldn't commit new state: Invalid argument".
# "couldn't compile atomic state", another errno, and the same words inside
# quotes do not match. No hit within 5s falls through; that is not a skip.
# shell exit on this SKIP is 0 (not 77). Step 4 does not run after it.
wake_cur=$("$QDWIN_VM_EXEC" "$VMNAME" 'cat /tmp/05-wake.cur 2>/dev/null' || true)
[ -n "$wake_cur" ] || { echo "FAIL: missing pre-idle/wake compositor cursor"; exit 1; }
if noct_poll_dpms_on_atomic_einval "$wake_cur" 5; then
    echo "SKIP: virtio-gpu rejected the DPMS-on atomic commit (atomic: couldn't commit new state: Invalid argument)"
    exit 0
fi
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

This step does not run when step 3 exited 0 on the virtio-gpu DPMS-on
atomic EINVAL SKIP. On the capable-host path (that record absent), the
error-line assert below is unchanged.

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

On a host that can commit DPMS-on, all asserts in steps 1-4 pass (bar
visible in the top 31 px after wake, clean journal). The virtio-gpu
DPMS-on EINVAL record is neither a pass nor a fail of those asserts:
step 3 prints `SKIP:` and exits 0, and step 3's bar assert and step 4
do not run.

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

4. **DPMS-on atomic commit rejected (virtio-gpu)** — after the step-2
 cursor, `qdwin-compositor.service` logs `atomic: couldn't commit new
 state: Invalid argument`. That is a host GPU capability gap, the same
 class as `idleDpms!=true`. Step 3 prints `SKIP:` and exits 0. Do not
 keep polling screenshots for the bar. Step 4 does not run. A black
 screenshot is not this signal, and a different errno is not this skip.
