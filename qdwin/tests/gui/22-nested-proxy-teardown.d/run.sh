#!/bin/bash
# 22-nested-proxy-teardown.d/run.sh <vmname> — HOST runner of
# qdwin/tests/gui/22-nested-proxy-teardown.md. Setup, S1-S4 and Teardown, in
# ONE host bash process; every guest action is its own short vm-exec call and
# the cleanup trap lives HERE, on the host, so no guest shell exiting between
# steps can tear the scenario down.
#
# Why this file exists: the scenario used to live only as host-side code blocks
# in the markdown, and the GUI runner hand-translated them into a guest driver
# of its own. Those translations kept dropping the one host-only precondition:
# qdwin_prime_pointer, a QMP motion that must come from the host.
# gui-20260930T114916Z-4162368 is the worked example: the translated driver
# never primed the pointer, the popup probe exited 77 "no pointer on the seat"
# 1 ms after creating its proxy (journal: created handle=3 12:10:54.284,
# destroy handle=3 12:10:54.285), and the S3 preview taken seconds later was
# legitimately black -- then reported as "captured while the probe was still
# waiting". The same translation also dropped the "probe already exited"
# guard below. Nothing about the product or the capture path was wrong:
# re-run live on the preserved disk, unprimed = rc=77 + black frame, primed =
# teal band at CLICK_TARGET + #333847 body.
#
# Needs QCI_GUI_ARTIFACT_DIR. Writes there: asserts.tsv (one row per assertion), s3-preview.png, s4-typed.png,
# summary.txt. Ends with `RESULT <PASS|FAIL|ERROR>` and exits 0 / 1 / 3.
# ERROR wins over FAIL (a FAIL in a run that did not complete is listed, but
# the class is ERROR). Assertion 4.3 (the typed text in s4-typed.png) is the
# one pixel assertion this script cannot decide: it is recorded as VISUAL and
# the runner grades it by opening the frame.
#
# Test seam (tests/integration/qci/qdwin-gui22-runner.bats): QD22_RUNSH_LIB=1
# makes `source run.sh` define the functions and return before doing anything.

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

# ---------------------------------------------------------------- recording
# assert <id> <PASS|FAIL|ERROR|SKIP|VISUAL> <detail>
qd22_assert() {
    printf 'ASSERT %s %s %s\n' "$1" "$2" "$3"
    [ -n "${QD22_ASSERTS:-}" ] && printf '%s\t%s\t%s\n' "$1" "$2" "$3" >>"$QD22_ASSERTS"
    return 0
}
# Stop the run: record the ERROR, and let the EXIT trap clean up and summarise.
qd22_abort() {   # qd22_abort <id> <detail>
    qd22_assert "$1" ERROR "$2"
    exit 3
}

# ---------------------------------------------------------------- probe I/O
# Run one probe mode as admin in the session; prints its output plus `rc=<n>`.
qd22_probe() {   # $@ = probe args
    "$QDWIN_VM_EXEC" "$VMNAME" \
      "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
         WAYLAND_DISPLAY=$ACTIVE_SOCKET \
         qdwin-nested-probe $* 2>&1; echo rc=\$?"
}
# The probe's status from qd22_probe's output: the LAST `rc=` line, digits only.
qd22_rc_of() {   # qd22_rc_of <output>
    printf '%s\n' "$1" | sed -nE 's/^rc=([0-9]+)$/\1/p' | tail -1
}

# Read the compositor journal after a cursor into QD22_JOURNAL. Returns
# nonzero when the READ failed: a failed remote read produces no stdout, and
# piping it straight into grep makes that indistinguishable from a clean read
# (proxy-lane-review-r4.md finding 3).
qd22_journal_since() {   # qd22_journal_since <cursor>
    QD22_JOURNAL=$("$QDWIN_VM_EXEC" "$VMNAME" \
      "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
         journalctl --user -b -u qdwin-compositor.service \
         --after-cursor '$1' --no-pager -o cat")
}

# Crash markers in the journal since <cursor>, as assertion <id>.
qd22_assert_no_fault() {   # <id> <cursor>
    if ! qd22_journal_since "$2"; then
        qd22_assert "$1" ERROR "could not read the compositor journal -- assertion not made"
    elif printf '%s\n' "$QD22_JOURNAL" | grep -E 'SIGSEGV|use-after-free|double free|Assertion'; then
        qd22_assert "$1" FAIL "compositor journal has a fault line (above)"
    else
        qd22_assert "$1" PASS "no SIGSEGV/use-after-free/double free/assertion since the step began"
    fi
}

qd22_assert_same_compositor() {   # <id>
    local now; now=$(qdwin_compositor_pid)
    if [ -z "$now" ]; then
        qd22_assert "$1" FAIL "no compositor pid (was $COMP_PID_BEFORE)"
    elif [ "$now" != "$COMP_PID_BEFORE" ]; then
        qd22_assert "$1" FAIL "compositor pid $COMP_PID_BEFORE -> $now (crash + restart)"
    else
        qd22_assert "$1" PASS "compositor pid unchanged ($now)"
    fi
}

# Reap S3's popup probe. Exit 0 only when nothing of ours can be running.
#
# The hard case is a launcher that has been BACKGROUNDED but not yet reached
# its publication step: an absent pid-file then means "pending", not "never
# started", and cleanup that returns success there lets the probe start
# afterwards and hold the shell role (r4 finding 1, reproduced by pausing the
# launcher). The launcher and the reaper therefore both check both flags:
#   - the launcher tests $QD22_CANCEL before publishing AND again immediately
#     after, removing its pid-file and exiting rather than starting the probe;
#   - the reaper sets $QD22_CANCEL first, then watches for a late pid.
# One of the two always observes the other, so after this returns 0 no probe
# can start. $QD22_INTENT distinguishes "never launched" from "launched".
# Capture through a FILE, never with a bare trailing `2>&1`. That merges
# vm-exec's stderr into fd 1, so if any caller ever wraps this function in
# `$( ... )` the descendants inherit the substitution PIPE on fd 2 and a
# survivor holds the call open forever. (No caller does today; the shape is
# removed rather than relied on.) The capture is per call and unlinked before
# the command starts.
# The read is ceilinged in BYTES, with `head -c`. It was `read -N` until
# 2026-09-17, which ceilings CHARACTERS: bash drops NUL bytes and they do not
# count, so a NUL-bearing capture was read PAST the ceiling. The claim that
# this "cannot hang, because a read of a regular file returns at the current
# EOF" was also wrong -- EOF is re-tested on every read, and a nominal 256 KiB
# replay was measured at 9.57s against a writer staying ahead of it.
QD22_CAP_BYTES=${QD22_CAP_BYTES:-65536}
# Not a positive decimal integer => not a byte ceiling: `head -c -1` is
# "all but the last byte" and `head -c 00` reads nothing.
case "$QD22_CAP_BYTES" in
    ''|*[!0-9]*|0|0*)
        echo "QD22_CAP_BYTES must be a positive integer number of bytes, got '$QD22_CAP_BYTES'" >&2
        exit 2 ;;
esac
qd22_reap_probe() {
    local cf wfd rfd out="" rc=0 _qc_size=""
    cf=$(mktemp "${TMPDIR:-/tmp}/qd22-reap.XXXXXXXX") || return 125
    # Every setup step is CHECKED. Unchecked, a failing open or unlink let this
    # function run the command anyway and return its status, leaving the capture
    # NAMED for the whole run -- the opposite of what the unlink is for.
    # Reproduced by injecting a failing rm: it returned 0, printed COMMAND-RAN
    # and left the file (sol, qci-A-260917-sol-review.md section 3).
    exec {wfd}>"$cf" || { rm -f "$cf"; return 125; }
    exec {rfd}<"$cf" || { exec {wfd}>&-; rm -f "$cf"; return 125; }
    rm -f "$cf" || { exec {wfd}>&- {rfd}<&-; return 125; }
    "$QDWIN_VM_EXEC" "$VMNAME" \
      "touch $QD22_CANCEL || { echo 'could not record cancellation'; exit 1; }; \
       [ -e $QD22_INTENT ] || { echo 'probe never launched'; exit 0; }; \
       p=''; \
       for _i in \$(seq 1 40); do \
         p=\$(cat $QD22_PID 2>/dev/null); \
         case \"\$p\" in ''|*[!0-9]*) sleep 0.1; continue;; esac; \
         break; \
       done; \
       case \"\$p\" in ''|*[!0-9]*) \
         echo 'cancellation recorded; no pid published'; exit 0;; \
       esac; \
       kill -TERM -\"\$p\" 2>/dev/null; \
       for _i in \$(seq 1 40); do kill -0 -\"\$p\" 2>/dev/null || { rm -f $QD22_PID; echo \"group \$p reaped\"; exit 0; }; sleep 0.1; done; \
       kill -KILL -\"\$p\" 2>/dev/null; \
       for _i in \$(seq 1 20); do kill -0 -\"\$p\" 2>/dev/null || { rm -f $QD22_PID; echo \"group \$p killed\"; exit 0; }; sleep 0.1; done; \
       echo \"probe group \$p SURVIVED\"; exit 1" >&"$wfd" 2>&"$wfd" {wfd}>&- {rfd}<&- || rc=$?
    exec {wfd}>&-
    if ! out=$(head -c "$QD22_CAP_BYTES" <&"$rfd"); then
        echo "capture replay FAILED; output unavailable, not empty" >&2
        exec {rfd}<&-
        return 125
    fi
    # THIS HELPER IS DIAGNOSTIC-ONLY, and that changes the failure policy.
    #
    # The comment that stood here was copied from the qd_cap/qs_ipc helpers in
    # the sibling scenarios and described THEIR situation: a `no running
    # instance|No such` PID fallback and state/geometry tokens parsed out of
    # the reply. This function has neither. Its two callers -- the cleanup
    # block and the S3/S4 transition -- read its EXIT STATUS and never parse
    # its output; the guest command establishes cancellation and reaping
    # through its own status, and its printed sentences are for a human
    # reading the log (astra, A7 finding 2).
    #
    # So a lost SIZE observation must not be reported as a failed reap. With
    # `stat` failing, the previous version returned 125 while cancellation had
    # in fact been recorded and the command had succeeded, and the callers
    # then announced "could not reap the popup probe" and "probe still holds
    # the shell role" -- neither of which followed from a diagnostic fault.
    # This is the same distinction as the await_vmexec_success exception.
    #
    # The byte bound stays, and an oversized capture is still refused: a
    # truncated diagnostic is worth saying out loud even here.
    #
    # NOT DETECTED, as everywhere else: a writer that hit its own
    # RLIMIT_FSIZE, handled EFBIG and exited zero. Comparing against this
    # process's `ulimit -f -H` cannot catch that -- `-H` is the hard limit
    # while writes obey the soft one, and the writer is a child whose limit
    # this process does not know (astra, A6 findings 1 and 2).
    if ! _qc_size=$(stat -Lc %s "/proc/self/fd/$rfd" 2>/dev/null); then
        echo "WARNING: could not stat the reap capture; the diagnostic below may be short. The cancellation/reap verdict is the command's own exit status and is unaffected." >&2
        exec {rfd}<&-
        printf '%s' "$out"
        return "$rc"
    fi
    if [ "$_qc_size" -gt "$QD22_CAP_BYTES" ]; then
        echo "capture (${_qc_size} bytes) exceeded $QD22_CAP_BYTES bytes; reply INCOMPLETE" >&2
        exec {rfd}<&-
        return 125
    fi
    exec {rfd}<&-
    printf '%s\n' "$out"
    return "$rc"
}

# ONE cleanup handler, installed as the EXIT trap right after the shell role is
# taken. Do not add a second one: an earlier draft replaced it in Teardown and
# silently dropped the terminal cleanup (proxy-lane-review-r4.md finding 2).
qd22_cleanup() {
    local reaped=ok
    if [ -n "${QD22_RUN:-}" ]; then
        qd22_reap_probe || { reaped=failed
            qd22_assert T.0 FAIL "could not reap the popup probe -- it may still hold the shell role"; }
        # S4's terminal, matched on the per-run title. The bracket stops `pkill -f`
        # matching the guest-agent shell running it; harmless if S4 never ran.
        "$QDWIN_VM_EXEC" "$VMNAME" \
          "pkill -u admin -f \"qd22-af[t]er-$QD22_RUN\" 2>/dev/null" >/dev/null 2>&1 || true
    fi
    if [ "$reaped" = failed ]; then
        # Restore anyway so the desktop is not left headless, but never let a
        # recovery that ran with ownership unresolved read as a clean exit.
        qd22_assert T.3 FAIL "restoring qdshell with probe ownership UNRESOLVED -- recovery, not a pass"
    fi
    if qdwin_apps_restore_shell; then
        qd22_assert T.2 PASS "qdshell.service restored and owns the shell role"
    else
        qd22_assert T.2 FAIL "qdshell restore failed"
    fi
    # T.1: the final handoff is part of what this lane exercises.
    [ -n "${COMP_PID_BEFORE:-}" ] && qd22_assert_same_compositor T.1
    # The cancel flag is a TOMBSTONE and is deliberately NOT removed: a launcher
    # descheduled before publication can still resume after this returns, and
    # its post-publication check needs the flag (proxy-lane-review-r5.md
    # finding 1). The files are per-invocation, in the VM's /tmp.
    return 0
}

qd22_summary() {
    local result=PASS n_fail
    n_fail=$(grep -c $'\tFAIL\t' "$QD22_ASSERTS" 2>/dev/null) || n_fail=0
    if grep -q $'\tERROR\t' "$QD22_ASSERTS" 2>/dev/null || [ "${QD22_DONE:-0}" != 1 ]; then
        result=ERROR
    elif [ "$n_fail" -gt 0 ]; then
        result=FAIL
    fi
    {
        echo "# 22-nested-proxy-teardown -- run.sh summary"
        echo "## assertions (asserts.tsv)"
        if [ -s "$QD22_ASSERTS" ]; then tr '\t' ' ' <"$QD22_ASSERTS"; else echo "(none)"; fi
        [ "${QD22_DONE:-0}" = 1 ] || echo "note: the run stopped before S4 finished -- see run.log"
        [ "$result" != ERROR ] || [ "$n_fail" -eq 0 ] || echo "note: $n_fail FAIL row(s) above, in a run that did not complete"
        echo "## frames to open and grade"
        ls -1 "$ART"/*.png 2>/dev/null || echo "(none)"
        echo "RESULT $result"
    } >"$ART/summary.txt"
    cat "$ART/summary.txt"
    case $result in PASS) return 0 ;; FAIL) return 1 ;; *) return 3 ;; esac
}

qd22_on_exit() {
    local rc
    trap - EXIT
    qd22_cleanup
    qd22_summary; rc=$?
    exit "$rc"
}

# ------------------------------------------------------------------ S3 parts
# Stage the popup launcher: a guest script from a QUOTED heredoc, so `$$` and
# `$?` reach the guest shell exactly as written and there is no escape layer
# to re-type (full-20260930T051422Z-65193 transcribed an inline
# `sh -c '... echo rc=\$? ...'` with one backslash too many and the log said a
# literal `rc=$?`). Per-run paths are arguments, never interpolated.
qd22_s3_stage() {
    local b64
    b64=$(base64 -w0 <<'SH'
#!/bin/sh
# usage: launch.sh <cancel-flag> <pid-file> <probe-log> <output>
[ -e "$1" ] && exit 91
echo $$ > "$2.tmp" && mv "$2.tmp" "$2" || exit 90
[ -e "$1" ] && { rm -f "$2"; exit 91; }
qdwin-nested-probe --destroy-with-popup --click-timeout 120 --output "$4" >"$3" 2>&1
echo "rc=$?" >>"$3"
SH
)
    "$QDWIN_VM_EXEC" "$VMNAME" "rm -f $QD22_LOG $QD22_LAUNCH_LOG $QD22_LAUNCHER $QD22_PID $QD22_CANCEL $QD22_INTENT" >/dev/null \
        || { echo "could not clear per-run state in the VM"; return 1; }
    "$QDWIN_VM_EXEC" "$VMNAME" "echo $b64 | base64 -d > $QD22_LAUNCHER && chmod 0755 $QD22_LAUNCHER" >/dev/null \
        || { echo "could not stage the popup launcher in the VM"; return 1; }
    # SYNCHRONOUS: after this returns, an absent pid means "pending", not "never".
    "$QDWIN_VM_EXEC" "$VMNAME" "touch $QD22_INTENT" >/dev/null \
        || { echo "could not record launch intent in the VM"; return 1; }
}

# Launch it DETACHED. vm-exec runs through qga guest-exec with capture-output,
# and qga reports a command finished only once EVERY holder of its stdout/
# stderr pipes has closed them; without the `</dev/null >$QD22_LAUNCH_LOG 2>&1`
# the backgrounded launcher keeps those pipes and this call does not return
# until the PROBE exits -- after its click timeout, with the proxy destroyed
# (every black S3 preview 2026-09-17..09-24; measured live 2026-09-25). The
# launcher's own output goes to a REGULAR FILE so an error before the probe
# starts (runuser, env, setsid, the pid publication) is not lost.
qd22_s3_launch() {
    "$QDWIN_VM_EXEC" "$VMNAME" \
      "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
         WAYLAND_DISPLAY=$ACTIVE_SOCKET \
         setsid sh $QD22_LAUNCHER $QD22_CANCEL $QD22_PID $QD22_LOG $QD22_OUTPUT \
           </dev/null >$QD22_LAUNCH_LOG 2>&1 &" \
      >/dev/null
}

# Acknowledge ownership (the published group pid) before anything can fail.
qd22_s3_ack() {
    PROBE_PID=
    for _ in $(seq 1 40); do
        PROBE_PID=$("$QDWIN_VM_EXEC" "$VMNAME" "cat $QD22_PID 2>/dev/null")
        [ -n "$PROBE_PID" ] && return 0
        sleep 0.25
    done
    echo "probe never published its pid within 10s; launcher output:"
    "$QDWIN_VM_EXEC" "$VMNAME" "cat $QD22_LAUNCH_LOG 2>&1"
    return 1
}

# Wait for CLICK_TARGET (trailing space: not CLICK_TARGET_GLOBAL, which is in
# global space and would aim outside the scanout on a head with an origin).
# The probe prints it only once the seat HAS a pointer and it is about to wait
# for the click (qdwin-nested-probe.c); an rc= first means it exited without
# one, so stop waiting as soon as either appears.
qd22_s3_target() {
    TARGET=
    for _ in $(seq 1 40); do
        TARGET=$("$QDWIN_VM_EXEC" "$VMNAME" "grep -m1 '^CLICK_TARGET ' $QD22_LOG 2>/dev/null")
        [ -n "$TARGET" ] && return 0
        "$QDWIN_VM_EXEC" "$VMNAME" "grep -q '^rc=' $QD22_LOG" && break
        sleep 0.5
    done
    echo "probe never printed CLICK_TARGET; probe log:"
    "$QDWIN_VM_EXEC" "$VMNAME" "cat $QD22_LOG; echo '--- launcher:'; cat $QD22_LAUNCH_LOG"
    return 1
}

# Is the popup probe still WAITING (no rc= yet)? It destroys the proxy when it
# exits, so a frame taken after rc= is legitimately black and grades nothing.
# An older probe printed CLICK_TARGET BEFORE its pointer check, so this guard
# is what stood between that and a black preview; keep it.
qd22_s3_waiting() {
    if "$QDWIN_VM_EXEC" "$VMNAME" "grep -q '^rc=' $QD22_LOG"; then
        echo "the popup probe already exited (proxy destroyed); any S3 frame now would be black. Probe log:"
        "$QDWIN_VM_EXEC" "$VMNAME" "cat $QD22_LOG"
        return 1
    fi
}

# rgb <png> <x> <y> -> "R G B" (0..255). ImageMagick's %[pixel:] can print a
# colour NAME ("black", "teal"), so read the channels numerically.
qd22_rgb() {
    magick "$1" -alpha off -format \
        "%[fx:int(255*p{$2,$3}.r+0.5)] %[fx:int(255*p{$2,$3}.g+0.5)] %[fx:int(255*p{$2,$3}.b+0.5)]" info:
}
# near <"R G B"> <R> <G> <B> [tol=24]
qd22_near() {
    local r g b tol=${5:-24} d
    [[ "$1" =~ ^[0-9]+\ [0-9]+\ [0-9]+$ ]] || return 2
    read -r r g b <<<"$1"
    for d in $(( r - $2 )) $(( g - $3 )) $(( b - $4 )); do
        [ "${d#-}" -le "$tol" ] || return 1
    done
}

# Grade the S3 preview's PIXELS where the probe says the proxy is, in the
# head's local space: the chrome band at CLICK_TARGET must be the probe's teal
# (#00aaaa) and the proxy body's centre its grey-blue (#333847). Prints one
# line; returns 0 both seen, 1 not seen, 2 undecidable (decode/parse failed).
# <png> <PROXY_GEOM line> <cx> <cy>
qd22_s3_pixels() {
    local png=$1 geom=$2 cx=$3 cy=$4 x y w h ox oy bx by band="" body="" v
    x=$(printf '%s' "$geom" | sed -nE 's/.* x=(-?[0-9]+) .*/\1/p')
    y=$(printf '%s' "$geom" | sed -nE 's/.* y=(-?[0-9]+) .*/\1/p')
    w=$(printf '%s' "$geom" | sed -nE 's/.* w=([0-9]+) .*/\1/p')
    h=$(printf '%s' "$geom" | sed -nE 's/.* h=([0-9]+) .*/\1/p')
    ox=$(printf '%s' "$geom" | sed -nE 's/.* out=[0-9]+x[0-9]+@(-?[0-9]+),-?[0-9]+ .*/\1/p')
    oy=$(printf '%s' "$geom" | sed -nE 's/.* out=[0-9]+x[0-9]+@-?[0-9]+,(-?[0-9]+) .*/\1/p')
    for v in "$x" "$y" "$w" "$h" "$ox" "$oy" "$cx" "$cy"; do
        [ -n "$v" ] || { echo "cannot parse PROXY_GEOM/CLICK_TARGET: '$geom' ($cx,$cy)"; return 2; }
    done
    bx=$(( x - ox + w / 2 )); by=$(( y - oy + h / 2 ))
    if ! band=$(qd22_rgb "$png" "$cx" "$cy" 2>&1) || ! body=$(qd22_rgb "$png" "$bx" "$by" 2>&1) \
       || ! [[ "$band $body" =~ ^[0-9]+\ [0-9]+\ [0-9]+\ [0-9]+\ [0-9]+\ [0-9]+$ ]]; then
        echo "could not decode $png at ($cx,$cy)/($bx,$by): '${band:-}' '${body:-}'"
        return 2
    fi
    if qd22_near "$band" 0 170 170 && qd22_near "$body" 51 56 71; then
        echo "chrome band at ($cx,$cy) = rgb($band) ~ #00aaaa; proxy body at ($bx,$by) = rgb($body) ~ #333847"
        return 0
    fi
    echo "proxy NOT in frame: chrome band at ($cx,$cy) = rgb($band) (want ~#00aaaa), body at ($bx,$by) = rgb($body) (want ~#333847)"
    return 1
}

[ "${QD22_RUNSH_LIB:-0}" = 1 ] && return 0

# ==================================================================== main
set -u
VMNAME=${1:-${VMNAME:-}}
[ -n "$VMNAME" ] || { echo "usage: run.sh <vmname>" >&2; exit 3; }
ART=${QCI_GUI_ARTIFACT_DIR:?run.sh needs QCI_GUI_ARTIFACT_DIR}
mkdir -p "$ART" || exit 3
QD22_ASSERTS=$ART/asserts.tsv
: >"$QD22_ASSERTS" || exit 3
rm -f "$ART/summary.txt" "$ART/s3-preview.png" "$ART/s4-typed.png"
QD22_DONE=0
# Until the shell role is taken there is nothing to clean up, only a summary.
trap 'trap - EXIT; qd22_summary; exit $?' EXIT

QDWIN_REPO=$(cd -- "$HERE/../../.." && pwd)
# shellcheck source=../qdwin-helpers.sh
source "$QDWIN_REPO/tests/gui/qdwin-helpers.sh" || { echo "ERROR: cannot source qdwin-helpers.sh"; exit 3; }
# shellcheck source=../../apps/qdwin-apps-helpers.sh
source "$QDWIN_REPO/tests/apps/qdwin-apps-helpers.sh" || { echo "ERROR: cannot source qdwin-apps-helpers.sh"; exit 3; }
qdwin_set_vm "$VMNAME"
qdwin_apps_set_vm "$VMNAME"
command -v magick >/dev/null 2>&1 || { qd22_assert 0.0 ERROR "ImageMagick 'magick' not on the host (S3's pixel gate needs it)"; exit 3; }

# The GUI lane pins Virtual-1 to 1280x800 (scripts/vm/spin-test-vm-gui.sh,
# weston.ini mode=1280x800@60); qdwin-helpers.sh and vm-gui default to the same.
# Assert 3.3 checks the probe's `out=` against these.
: "${QDWIN_SCREEN_W:=1280}"
: "${QDWIN_SCREEN_H:=800}"
export QDWIN_SCREEN_W QDWIN_SCREEN_H
# The head S3 aims at: the DRM scanout that QEMU's tablet spans, the one qdwin
# pins shell capture to and the one the lane screenshots. The golden also
# advertises pipewire-0/pipewire-1, which take no seat input.
: "${QD22_OUTPUT:=Virtual-1}"

# ------------------------------------------------------------------ Setup
qdwin_session_healthy || { qd22_assert 0.0 ERROR "qdwin/qdshell user session not up"; exit 3; }

# The probe must be installed with the --destroy-with-* modes and output-aware
# click calibration (--output). Absence is ERROR (stale deployment), never FAIL.
"$QDWIN_VM_EXEC" "$VMNAME" 'command -v qdwin-nested-probe >/dev/null' \
    || { qd22_assert 0.0 ERROR "qdwin-nested-probe not installed on VM"; exit 3; }
"$QDWIN_VM_EXEC" "$VMNAME" 'qdwin-nested-probe --help 2>&1 | grep -q -- --destroy-with-stream' \
    || { qd22_assert 0.0 ERROR "installed qdwin-nested-probe lacks the --destroy-with-* modes"; exit 3; }
"$QDWIN_VM_EXEC" "$VMNAME" 'qdwin-nested-probe --help 2>&1 | grep -q -- --output' \
    || { qd22_assert 0.0 ERROR "installed qdwin-nested-probe predates multi-output click calibration (no --output); rebuild qdwin's test-client and rebake the golden"; exit 3; }

# The compositor identity, recorded ONCE before anything is torn down; a
# crash-and-restart is the failure this lane exists to catch.
COMP_PID_BEFORE=$(qdwin_compositor_pid)
[ -n "$COMP_PID_BEFORE" ] || { qd22_assert 0.0 ERROR "no compositor pid"; exit 3; }
echo "compositor pid before = $COMP_PID_BEFORE"

# Per-invocation guest state. `$$` alone repeats across reruns in one shell.
QD22_RUN="$$-$(date +%s)-$RANDOM"
QD22_LOG=/tmp/qd22-popup.$QD22_RUN.log
# The LAUNCHER's own output, apart from $QD22_LOG (the probe truncates that).
QD22_LAUNCH_LOG=/tmp/qd22-popup.$QD22_RUN.launch.log
QD22_LAUNCHER=/tmp/qd22-popup.$QD22_RUN.launch.sh
QD22_PID=/tmp/qd22-popup.$QD22_RUN.pid
QD22_CANCEL=/tmp/qd22-popup.$QD22_RUN.cancel
QD22_INTENT=/tmp/qd22-popup.$QD22_RUN.intent
QD22_DONE=0

# Free the singleton shell role for the probe (stops qdshell, evicts any suite
# bystander, waits for qdwin to log `shell unbound`), and arm the cleanup
# IMMEDIATELY after, so a later failure never leaves the desktop headless.
qdwin_apps_prepare_shell_probe \
    || { qd22_assert 0.1 ERROR "could not reserve the singleton shell role"; exit 3; }
trap qd22_on_exit EXIT
trap 'exit 3' TERM INT HUP
qd22_assert 0.1 PASS "shell role free (compositor logged 'shell unbound')"

# The seat must ADVERTISE a pointer before S1/S3 ask for one. libweston 16 adds
# the pointer capability lazily, on a pointer device's first event, so a fresh
# worker whose tablet never moved advertises a keyboard-only seat and the probe
# exits 77 "no pointer on the seat". This is a HOST action (QMP), which is why
# this scenario is run from the host: the hand-translated guest drivers that
# dropped it produced every "black S3 preview" of 2026-09-30. Park the pointer
# bottom-right -- outside the proxy's 800x600 at (240,100) -- AFTER qdshell is
# stopped, so nothing reacts to the hover.
qdwin_prime_pointer \
    || qd22_abort 0.2 "could not inject the pointer-priming motion over QMP"
qd22_assert 0.2 PASS "pointer primed over QMP"

# Never hard-code wayland-1: the socket name moves across compositor restarts.
ACTIVE_SOCKET=$(qdwin_apps_active_socket)
[ -n "$ACTIVE_SOCKET" ] || qd22_abort 0.3 "qdwin stopped during shell handoff"

# ------------------------------------------------------------------ S1
echo "=== S1: destroy under a live move-drag"
CURSOR=$(qdwin_apps_journal_cursor)
[ -n "$CURSOR" ] || qd22_abort 1.0 "empty compositor journal cursor"
S1_OUT=$(qd22_probe --destroy-with-move)
printf '%s\n' "$S1_OUT"
S1_RC=$(qd22_rc_of "$S1_OUT")
case $S1_RC in
    0)  if printf '%s\n' "$S1_OUT" | grep -q 'proxy destroyed under a live move-drag; compositor alive'; then
            qd22_assert 1.1 PASS "rc=0 and the move-drag teardown line"
        else
            qd22_assert 1.1 FAIL "rc=0 but no 'proxy destroyed under a live move-drag; compositor alive' line"
        fi ;;
    77) qd22_assert 1.1 ERROR "rc=77 (inconclusive) after pointer priming on the DRM session: an environment fault, see the probe line above" ;;
    '') qd22_assert 1.1 ERROR "no numeric rc= from the probe (vm-exec failed?)" ;;
    *)  qd22_assert 1.1 FAIL "rc=$S1_RC" ;;
esac
qd22_assert_same_compositor 1.2
qd22_assert_no_fault 1.3 "$CURSOR"

# ------------------------------------------------------------------ S2
echo "=== S2: destroy under a LIVE view_stream"
CURSOR=$(qdwin_apps_journal_cursor)
[ -n "$CURSOR" ] || qd22_abort 2.0 "empty compositor journal cursor"
S2_OUT=$(qd22_probe --destroy-with-stream)
printf '%s\n' "$S2_OUT"
S2_RC=$(qd22_rc_of "$S2_OUT")
case $S2_RC in
    0)  qd22_assert 2.1 PASS "rc=0" ;;
    77) qd22_assert 2.1 ERROR "rc=77: the stream precondition was not established (no free pipewire output, spawn failure, or torn down before the destroy) -- see the probe line above" ;;
    '') qd22_assert 2.1 ERROR "no numeric rc= from the probe (vm-exec failed?)" ;;
    *)  qd22_assert 2.1 FAIL "rc=$S2_RC" ;;
esac
if printf '%s\n' "$S2_OUT" | grep -q 'torn_down reason="source toplevel closed"'; then
    qd22_assert 2.2 PASS 'torn_down reason="source toplevel closed"'
elif [ "$S2_RC" = 0 ]; then
    qd22_assert 2.2 FAIL 'rc=0 but no torn_down reason="source toplevel closed" line'
else
    qd22_assert 2.2 ERROR "not decided: the probe did not complete (rc=${S2_RC:-none})"
fi
qd22_assert_same_compositor 2.3
if ! qd22_journal_since "$CURSOR"; then
    qd22_assert 2.4 ERROR "could not read the compositor journal"
elif printf '%s\n' "$QD22_JOURNAL" | grep -q 'qdwin: view_stream_server_state_released'; then
    qd22_assert 2.4 PASS "view_stream_server_state_released logged"
elif [ "$S2_RC" = 0 ]; then
    qd22_assert 2.4 FAIL "no view_stream_server_state_released since the step began"
else
    qd22_assert 2.4 ERROR "not decided: the probe did not complete (rc=${S2_RC:-none})"
fi
qd22_assert_no_fault 2.5 "$CURSOR"

# ------------------------------------------------------------------ S3
echo "=== S3: destroy under a LIVE chrome popup"
CURSOR=$(qdwin_apps_journal_cursor)
[ -n "$CURSOR" ] || qd22_abort 3.0 "empty compositor journal cursor"
msg=$(qd22_s3_stage) || qd22_abort 3.0 "$msg"
S3_LAUNCH_T0=$SECONDS
qd22_s3_launch
echo "S3 launch returned after $((SECONDS - S3_LAUNCH_T0))s (expected ~0s; as long as the click timeout means the launcher pinned vm-exec again)"
qd22_s3_ack || qd22_abort 3.0 "the popup probe never published its pid (launcher output above)"
echo "probe group pid=$PROBE_PID"
qd22_s3_target || qd22_abort 3.0 "the popup probe never printed CLICK_TARGET (probe log above; 'no pointer on the seat' means the priming did not take)"
GEOM=$("$QDWIN_VM_EXEC" "$VMNAME" "grep -m1 '^PROXY_GEOM' $QD22_LOG")
echo "$GEOM"
echo "$TARGET"
CX=$(printf '%s' "$TARGET" | sed -nE 's/.*x=(-?[0-9]+).*/\1/p')
CY=$(printf '%s' "$TARGET" | sed -nE 's/.*y=(-?[0-9]+).*/\1/p')

# 3.3 -- calibration, decided from the probe's own report of wl_output.
G_OUT=$(printf '%s' "$GEOM" | sed -nE 's/.* output=([^ ]+) .*/\1/p')
G_WH=$(printf '%s' "$GEOM" | sed -nE 's/.* out=([0-9]+x[0-9]+)@.*/\1/p')
G_ST=$(printf '%s' "$GEOM" | sed -nE 's/.* (scale=[0-9]+ transform=[0-9]+) .*/\1/p')
if [ "$G_OUT" = "$QD22_OUTPUT" ] && [ "$G_WH" = "${QDWIN_SCREEN_W}x${QDWIN_SCREEN_H}" ] \
   && [ "$G_ST" = "scale=1 transform=0" ]; then
    qd22_assert 3.3 PASS "output=$G_OUT out=$G_WH $G_ST"
else
    qd22_abort 3.3 "calibration mismatch: output=${G_OUT:-?} (want $QD22_OUTPUT) out=${G_WH:-?} (want ${QDWIN_SCREEN_W}x${QDWIN_SCREEN_H}) ${G_ST:-?} (want scale=1 transform=0) -- a click would land elsewhere; nothing about the teardown path can be tested"
fi

# The S3 frame: only while the probe is verifiably WAITING, both before and
# after the capture, so the frame is known to be taken with the proxy alive.
msg=$(qd22_s3_waiting) || qd22_abort 3.4 "$msg"
qdwin_apps_screenshot "$ART/s3-preview.png" || qd22_abort 3.4 "could not capture $ART/s3-preview.png"
msg=$(qd22_s3_waiting) || qd22_abort 3.4 "the probe exited DURING the capture; s3-preview.png is not gradable. $msg"
pix=$(qd22_s3_pixels "$ART/s3-preview.png" "$GEOM" "$CX" "$CY"); prc=$?
echo "$pix"
case $prc in
    0) qd22_assert 3.4 PASS "s3-preview.png, taken while the probe waited: $pix" ;;
    1) # The probe was waiting on both sides of the capture, so this is NOT the
       # old "frame after the proxy died". Record, and do not click blind.
       qd22_abort 3.4 "s3-preview.png was taken while the probe was verifiably waiting, and $pix. Open the frame; quote the 'created handle=' / 'destroy handle=' journal lines. Not clicking blind." ;;
    *) qd22_abort 3.4 "s3-preview.png undecidable: $pix" ;;
esac

echo "clicking chrome at ($CX, $CY)"
qdwin_click "$CX" "$CY" left
for _ in $(seq 1 40); do
    "$QDWIN_VM_EXEC" "$VMNAME" "grep -q '^rc=' $QD22_LOG" && break
    sleep 0.5
done
S3_LOG=$("$QDWIN_VM_EXEC" "$VMNAME" "cat $QD22_LOG")
printf '%s\n' "$S3_LOG"
S3_RC=$(qd22_rc_of "$S3_LOG")
if printf '%s\n' "$S3_LOG" | grep -qx 'rc=\$?'; then
    qd22_assert 3.1 ERROR 'the log says a literal rc=$? -- the launcher was not run as staged; no product verdict'
else
    case $S3_RC in
        0)  if printf '%s\n' "$S3_LOG" | grep -q 'proxy destroyed under a LIVE chrome popup; dismissed fired; compositor alive'; then
                qd22_assert 3.1 PASS "rc=0 and the popup teardown line"
            else
                qd22_assert 3.1 FAIL "rc=0 but no 'proxy destroyed under a LIVE chrome popup; dismissed fired; compositor alive' line"
            fi ;;
        77) qd22_assert 3.1 ERROR "rc=77 (precondition not established) -- see the probe line above; with 3.3 PASS, 'no chrome_button' after a click aimed at a teal band is a routing question, not a calibration one" ;;
        '') qd22_assert 3.1 ERROR "the probe had not exited 20s after the click (no rc=)" ;;
        *)  qd22_assert 3.1 FAIL "rc=$S3_RC" ;;
    esac
fi
qd22_assert_same_compositor 3.2

# The probe must be gone AND the compositor must have released its shell role
# before S4 claims it. qdwin_apps_restore_shell's own pre-start wait watches
# for a BYSTANDER unbind, which says nothing about this probe.
qd22_reap_probe || qd22_abort 3.5 "probe still holds the shell role; not proceeding to S4"
SHELL_FREE=0
for _ in $(seq 1 40); do
    "$QDWIN_VM_EXEC" "$VMNAME" \
      "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
         journalctl --user -b -u qdwin-compositor.service \
         --after-cursor '$CURSOR' --no-pager -o cat 2>/dev/null" \
      | grep -qE '^(\[[0-9:.]+\] )?qdwin: shell unbound$' && { SHELL_FREE=1; break; }
    sleep 0.25
done
[ "$SHELL_FREE" = 1 ] || qd22_abort 3.5 "compositor never reported 'shell unbound' after the probe"
qd22_assert_no_fault 3.6 "$CURSOR"

# ------------------------------------------------------------------ S4
echo "=== S4: KEYBOARD input still reaches an ordinary window"
QD22_TERM_TITLE="qd22-after-$QD22_RUN"
if ! "$QDWIN_VM_EXEC" "$VMNAME" 'command -v foot >/dev/null 2>&1'; then
    qd22_assert 4 SKIP "foot not installed (qdwin app deps are opt-in; rerun with QDWIN_APP_DEPS=1) -- with S4 skipped nothing here proves input survived the teardowns"
else
    CURSOR=$(qdwin_apps_journal_cursor)
    qdwin_apps_become_shell || qd22_abort 4.0 "could not take the shell role back"
    if ! qdwin_apps_session_up; then
        qd22_assert 4.0 FAIL "session not healthy after the teardowns"
        QD22_DONE=1
        exit 1
    fi
    qdwin_apps_launch qd22-after "foot --title $QD22_TERM_TITLE"
    # Identify THIS run's terminal by its PID, then its handle by the
    # compositor's own toplevel_added line for that pid. Not by title: foot
    # has no title yet when it maps (the bystander logs title="" -- see
    # tests/apps/03-foot-vs-xterm-tagging.md) and nothing logs later title
    # changes, so the title grep this step used to do could never match. Not
    # by app_id alone either: `tail -1` on app_id would select any terminal.
    # The bracket keeps pgrep -f from matching the guest-agent shell running it.
    FOOT_PID=
    HANDLE=
    for _ in $(seq 1 40); do
        FOOT_PID=$("$QDWIN_VM_EXEC" "$VMNAME" \
          "for p in \$(pgrep -u admin -f 'qd22-af[t]er-$QD22_RUN'); do \
             [ \"\$(cat /proc/\$p/comm 2>/dev/null)\" = foot ] && echo \$p; done | head -1")
        if [ -n "$FOOT_PID" ] && qd22_journal_since "$CURSOR"; then
            HANDLE=$(printf '%s\n' "$QD22_JOURNAL" \
              | sed -nE "s/.*qdwin: toplevel_added handle=([0-9]+) uid=[0-9]+ pid=$FOOT_PID( .*)?\$/\\1/p" | tail -1)
        fi
        [ -n "$HANDLE" ] && break
        sleep 0.5
    done
    if [ -n "$HANDLE" ]; then
        qd22_assert 4.1 PASS "post-teardown toplevel handle=$HANDLE (foot pid $FOOT_PID)"
        qdwin_apps_type "qdwinlives"
        sleep 1
        if qdwin_apps_screenshot "$ART/s4-typed.png"; then
            qd22_assert 4.3 VISUAL "open s4-typed.png: 'qdwinlives' must be echoed in the foot window (absent while the window is visible and focused = FAIL)"
        else
            qd22_assert 4.3 ERROR "could not capture $ART/s4-typed.png"
        fi
    else
        if [ -z "$FOOT_PID" ]; then
            qd22_assert 4.1 ERROR "foot (title qd22-after-$QD22_RUN) is not running in the guest -- see /tmp/qd22-after.log there; not a compositor verdict"
        else
            qd22_assert 4.1 FAIL "foot pid $FOOT_PID is running but the compositor logged no toplevel_added for it"
        fi
    fi
    qd22_assert_same_compositor 4.2
    qd22_assert_no_fault 4.4 "$CURSOR"
fi
QD22_DONE=1
exit 0
