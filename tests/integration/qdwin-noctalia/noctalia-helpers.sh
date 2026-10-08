# Helpers specific to driving the qdwin+qdshell session. Source this AFTER
# `qdwin/tests/gui/qdwin-helpers.sh`.
#
# Reuses every helper from qdwin-helpers.sh; adds session checks against
# the PRODUCTION deploy unit names (qdwin-compositor.service /
# qdshell.service / qdwin-session.target) so these lanes validate the
# units deploy actually ships. (The legacy noctalia-session /
# noctalia-shell names were retired 2026-06-16 — the dir name stays
# qdwin-noctalia for historical continuity; only the unit names changed.)

# Returns 0 if qdshell.service (admin's user unit) is active and the qs
# process is alive. Uses single-quoted commands to avoid vm-exec's
# JSON-quoting fragility (embedded " in the inner runuser command breaks
# the harness — see memory vm_exec_quoting_fragility).
noct_session_healthy() {
    qdwin_require_vm || return 2
    local out
    out=$("$QDWIN_VM_EXEC" "$VMNAME" \
        "su - admin -c 'systemctl --user is-active qdshell.service' && pgrep -f /usr/bin/qs >/dev/null && echo OK" \
        2>/dev/null | tail -1)
    [ "$out" = "OK" ]
}

# Move cursor + take a screenshot. Wraps qdwin_screenshot with an
# initial cursor wake so DPMS doesn't blank the panel.
noct_screenshot_awake() {
    local out="${1:-/tmp/noct-shot.png}"
    qdwin_mouse_move 800 400
    sleep 0.3
    qdwin_mouse_move 850 450
    sleep 0.5
    qdwin_screenshot "$out"
}

# Count the number of "qdwin: layer-shell mapped" entries in the
# session journal since a given systemd timestamp ("HH:MM:SS" or
# free-form). Useful for asserting the shell mapped the expected
# number of surfaces.
noct_layer_mapped_count_since() {
    local since="${1:-10 minutes ago}"
    "$QDWIN_VM_EXEC" "$VMNAME" \
        "runuser -l admin -c \"journalctl --user -u qdwin-compositor.service --since '$since' --no-pager\" | grep -c 'layer-shell mapped'" \
        2>/dev/null | tail -1
}

# Restart the shell cleanly (used when a scenario knocks it over). Restarts
# qdshell.service; its Requires=qdwin-compositor.service pulls the
# compositor back if it is also down.
noct_restart() {
    "$QDWIN_VM_EXEC" "$VMNAME" \
        'runuser -l admin -c "systemctl --user reset-failed qdshell.service qdwin-compositor.service && systemctl --user restart qdshell.service"' \
        >/dev/null
    sleep 8
}

# How long scenario 04 polls for a runtime cursor-plane remap, and how often.
# A fixed one-second sleep raced journal visibility under parallel GUI load,
# and under the 12-way full run the first remap landed ~10s after the move
# (full-20260930T051422Z-65193), so the bound is 30s. The poll returns on the
# first sighting, so a healthy run does not wait any longer.
: "${NOCT_CURSOR_WAIT_S:=30}"
: "${NOCT_CURSOR_POLL_S:=0.25}"

# Count `mapped on cursor_layer` remaps whose nonzero_alpha token is not 0.
# Reads a journal excerpt on stdin. nonzero_alpha=10 counts; nonzero_alpha=0,
# a negative value, and a line with no nonzero_alpha token do not. This is the
# predicate assert 1.1/2.1/3.1 in agent-cursor-tracking-smoke.sh checks.
noct_count_cursor_layer_nonzero_alpha() {
    awk '
        /mapped on cursor_layer/ {
            rest = $0
            while (match(rest, /nonzero_alpha=[0-9]+/)) {
                token = substr(rest, RSTART, RLENGTH)
                if (token != "nonzero_alpha=0") n++
                rest = substr(rest, RSTART + RLENGTH)
            }
        }
        END { print n + 0 }
    '
}

# Compositor user-journal text strictly after an opaque journalctl cursor.
# Production reads journalctl --after-cursor via vm-exec. When
# NOCT_CURSOR_JOURNAL_FILE is set (host tests, no VM), the file is a plain
# text stand-in and the cursor is a whole line `-- cursor: <token>`; only
# text after that line is returned, so a remap from before the move cannot
# satisfy the count.
noct_compositor_journal_after() {
    local cur="$1"
    if [ -n "${NOCT_CURSOR_JOURNAL_FILE:-}" ]; then
        awk -v cur="$cur" '
            $0 == "-- cursor: " cur { found = 1; buf = ""; next }
            found { buf = buf $0 "\n" }
            END { printf "%s", buf }
        ' "$NOCT_CURSOR_JOURNAL_FILE"
        return
    fi
    "$QDWIN_VM_EXEC" "$VMNAME" \
        "runuser -l admin -c \"journalctl --user -u qdwin-compositor.service --after-cursor '$cur' --no-pager\"" \
        2>/dev/null
}

# Runtime nonzero-alpha cursor remaps after a journal cursor captured
# immediately before the move. Prints a single integer.
cursor_layer_nonzero_alpha_after() {
    local cur="$1" filter="${2:-}"
    if [ -n "$filter" ]; then
        noct_compositor_journal_after "$cur" | grep -E -- "$filter" | noct_count_cursor_layer_nonzero_alpha
    else
        noct_compositor_journal_after "$cur" | noct_count_cursor_layer_nonzero_alpha
    fi
}

# Poll cursor_layer_nonzero_alpha_after until it is >= 1 or the deadline
# passes. $1 is the journal cursor; $2 overrides NOCT_CURSOR_WAIT_S (seconds;
# empty = default). $3, optional, is an ERE a counted line must ALSO match
# (e.g. 'cursor-shape install shape=default:'), so the shape and its nonzero
# alpha are read from the same line. Returns 0 on the first sighting. On
# expiry prints FAIL (bound, cursor, last count, journal tail) and returns 1.
# Each iteration re-reads the journal.
noct_wait_cursor_layer_nonzero_alpha() {
    local cur="$1"
    local timeout="${2:-$NOCT_CURSOR_WAIT_S}"
    local filter="${3:-}"
    local interval="$NOCT_CURSOR_POLL_S"
    local start_ms now_ms limit_ms elapsed_ms count text
    case "$timeout" in
        ''|*[!0-9.]*)
            echo "FAIL: bad cursor-journal wait bound '${timeout}'"
            return 1
            ;;
    esac
    case "$interval" in
        ''|*[!0-9.]*)
            echo "FAIL: bad cursor-journal poll interval '${interval}'"
            return 1
            ;;
    esac
    start_ms=$(date +%s%3N) || {
        echo "FAIL: date +%s%3N failed; cannot bound the cursor-journal wait"
        return 1
    }
    limit_ms=$(awk -v t="$timeout" 'BEGIN { printf "%d", t * 1000 }')
    while :; do
        text=""
        if text=$(noct_compositor_journal_after "$cur"); then
            :
        fi
        if [ -n "$filter" ]; then
            count=$(printf '%s\n' "$text" | grep -E -- "$filter" | noct_count_cursor_layer_nonzero_alpha)
        else
            count=$(printf '%s\n' "$text" | noct_count_cursor_layer_nonzero_alpha)
        fi
        case "$count" in
            ''|*[!0-9]*) count=0 ;;
        esac
        if [ "$count" -ge 1 ]; then
            return 0
        fi
        now_ms=$(date +%s%3N) || now_ms=$start_ms
        elapsed_ms=$(( now_ms - start_ms ))
        if [ "$elapsed_ms" -ge "$limit_ms" ]; then
            echo "FAIL: timed out after ${timeout}s waiting for mapped on cursor_layer with nonzero_alpha>0${filter:+ on a line matching '$filter'} (elapsed_ms=${elapsed_ms} cursor=${cur} last_count=${count})"
            printf '%s\n' "$text" | tail -n 12
            return 1
        fi
        sleep "$interval"
    done
}

# virtio-gpu DPMS-on rejection (libweston backend-drm/kms.c).
# weston_log("atomic: couldn't commit new state: %s\n", strerror(errno))
# with EINVAL ends the line in exactly:
#   atomic: couldn't commit new state: Invalid argument
# A journalctl prefix may precede "atomic:" (the byte before it is
# whitespace, or the line is only that message). Success (0) only for that
# record. Not a match: "couldn't compile atomic state", any other errno,
# the same words inside quotes, or a copy that does not end the line.
# Prints nothing. Reads journal text on stdin.
# shell exit on this SKIP is 0. Callers print a line starting with "SKIP:"
# and exit 0. Do not exit 77: the GUI harness records SKIP only with rc=0.
# Absence of the record is not a skip (non-zero, no output).
noct_dpms_on_atomic_einval() {
    local line trimmed prefix last
    local reason="atomic: couldn't commit new state: Invalid argument"
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        trimmed=${line%"${line##*[![:space:]]}"}
        [ -n "$trimmed" ] || continue
        case "$trimmed" in
            *"$reason") ;;
            *) continue ;;
        esac
        prefix=${trimmed%"$reason"}
        if [ -z "$prefix" ]; then
            return 0
        fi
        last=${prefix: -1}
        case "$last" in
            [[:space:]]) return 0 ;;
        esac
    done
    return 1
}

# Same record, strictly after an opaque journalctl cursor. Production reads
# `journalctl --user -u qdwin-compositor.service --after-cursor` (see
# noct_compositor_journal_after). A hit from before the cursor does not count.
noct_dpms_on_atomic_einval_after() {
    local cur="$1"
    noct_compositor_journal_after "$cur" | noct_dpms_on_atomic_einval
}

# How long scenario 05 polls for the DPMS-on EINVAL record after the wake
# move. Same window as that scenario's "black for >5s is acceptable".
: "${NOCT_DPMS_WAKE_WAIT_S:=5}"
: "${NOCT_DPMS_WAKE_POLL_S:=0.25}"

# Poll noct_dpms_on_atomic_einval_after until the record appears or $2
# seconds (default NOCT_DPMS_WAKE_WAIT_S) elapse. Returns 0 on the first
# sighting and prints nothing — the scenario prints "SKIP:" and exits 0.
# Returns 1 when the bound expires with no record. That is not a skip and
# does not end the scenario. An empty cursor is the caller's FAIL; this
# returns 1 without skipping.
noct_poll_dpms_on_atomic_einval() {
    local cur="$1"
    local timeout="${2:-$NOCT_DPMS_WAKE_WAIT_S}"
    local interval="${NOCT_DPMS_WAKE_POLL_S}"
    local start_ms now_ms limit_ms elapsed_ms
    [ -n "$cur" ] || return 1
    case "$timeout" in
        ''|*[!0-9.]*)
            echo "FAIL: bad DPMS-wake journal wait bound '${timeout}'"
            return 1
            ;;
    esac
    case "$interval" in
        ''|*[!0-9.]*)
            echo "FAIL: bad DPMS-wake journal poll interval '${interval}'"
            return 1
            ;;
    esac
    start_ms=$(date +%s%3N) || {
        noct_dpms_on_atomic_einval_after "$cur"
        return
    }
    limit_ms=$(awk -v t="$timeout" 'BEGIN { printf "%d", (t * 1000) + 0.5 }')
    while :; do
        if noct_dpms_on_atomic_einval_after "$cur"; then
            return 0
        fi
        now_ms=$(date +%s%3N) || return 1
        elapsed_ms=$(( now_ms - start_ms ))
        if [ "$elapsed_ms" -ge "$limit_ms" ]; then
            return 1
        fi
        sleep "$interval"
    done
}

# ---- Scenario 05 step 2: a guest-timed, detached idle wait ----------------
#
# The idle wait used to be a host `sleep 75` followed by a sysfs read, all in
# one driver tool call. In full-20261006T175536Z-3524705 the driver's tool
# returned before that call finished (step2-idle.log never got its dpms line),
# and the driver then read /sys/.../dpms itself ~50 s after its last pointer
# input -- before the 60 s display-off timeout -- and recorded FAIL "dpms
# stayed On". The product had blanked on time in every replay.
#
# Now the GUEST times the wait: noct_idle_wait_start launches a transient
# systemd unit that sleeps NOCT_IDLE_WAIT_S, reads the DRM connector, and
# atomically writes `dpms=<state> waited_s=<n>` to a fresh result path. The
# wait therefore cannot be cut short by the host side, and the record says how
# long it actually waited. The host polls the file with short vm-exec calls
# (no input: QGA only), and a poll that is itself interrupted can simply be
# re-run against the same result path.
: "${NOCT_IDLE_WAIT_S:=75}"
: "${NOCT_IDLE_POLL_S:=3}"
: "${NOCT_IDLE_POLL_MAX_S:=180}"
: "${NOCT_DPMS_SYSFS:=/sys/class/drm/card0-Virtual-1/dpms}"
# Guest-side scratch root for per-scenario wait records. In the VM this is the
# harness-created /tmp/qci (root-owned, mode 1777). Host-only bats override it:
# on a shared host /tmp/qci may belong to another user's run.
: "${NOCT_QCI_DIR:=/tmp/qci}"

# noct_idle_wait_script <result-path>: the guest script (pure; host-testable).
noct_idle_wait_script() {
    local res=$1
    printf '%s\n' \
        'start=$(date +%s)' \
        "sleep $NOCT_IDLE_WAIT_S" \
        "d=\$(cat $NOCT_DPMS_SYSFS 2>/dev/null || echo unreadable)" \
        'end=$(date +%s)' \
        "printf 'dpms=%s waited_s=%s\\n' \"\$d\" \"\$((end-start))\" > $res.tmp && mv -f $res.tmp $res"
}

# noct_idle_wait_start <result-path>: start the detached guest wait. The path
# must be fresh (per-attempt token) so a stale record can never be read.
noct_idle_wait_start() {
    local res=$1 b64
    case "$res" in
        "$NOCT_QCI_DIR"/*/*) ;;
        *) echo "FAIL: idle-wait result path must be under the per-scenario $NOCT_QCI_DIR dir, got '$res'"; return 2 ;;
    esac
    case "$NOCT_IDLE_WAIT_S" in ''|*[!0-9]*) echo "FAIL: bad NOCT_IDLE_WAIT_S '$NOCT_IDLE_WAIT_S'"; return 2 ;; esac
    b64=$(noct_idle_wait_script "$res" | base64 -w0) || return 2
    # Single quotes only: vm-exec's qga JSON breaks on an embedded double quote.
    "$QDWIN_VM_EXEC" "$VMNAME" \
        "test ! -e $res && systemd-run --quiet --collect --unit=qci-noct-idle-wait-\$(date +%s%N) /bin/sh -c 'echo $b64 | base64 -d | sh'"
}

# noct_idle_wait_poll <result-path>: print the record once the guest wrote it.
# Returns 0 with the record on stdout, 1 if none appeared within
# NOCT_IDLE_POLL_MAX_S. Each probe is one short vm-exec call.
noct_idle_wait_poll() {
    local res=$1 out start now
    start=$(date +%s)
    while :; do
        out=$("$QDWIN_VM_EXEC" "$VMNAME" "cat $res 2>/dev/null" 2>/dev/null | grep -E '^dpms=' | tail -1) || true
        if [ -n "$out" ]; then printf '%s\n' "$out"; return 0; fi
        now=$(date +%s)
        [ $((now - start)) -lt "$NOCT_IDLE_POLL_MAX_S" ] || return 1
        sleep "$NOCT_IDLE_POLL_S"
    done
}

# noct_idle_wait_verdict <record>: 0 iff the guest waited at least
# NOCT_IDLE_WAIT_S and then read exactly `Off`. Prints the reason otherwise.
noct_idle_wait_verdict() {
    local rec=$1 d w
    d=$(printf '%s\n' "$rec" | sed -n 's/^dpms=\([^ ]*\) waited_s=\([0-9][0-9]*\)$/\1/p')
    w=$(printf '%s\n' "$rec" | sed -n 's/^dpms=\([^ ]*\) waited_s=\([0-9][0-9]*\)$/\2/p')
    [ -n "$d" ] && [ -n "$w" ] || { echo "malformed idle-wait record '$rec'"; return 1; }
    [ "$w" -ge "$NOCT_IDLE_WAIT_S" ] || { echo "guest waited only ${w}s (< ${NOCT_IDLE_WAIT_S}s)"; return 1; }
    [ "$d" = Off ] || { echo "DPMS read '$d' after ${w}s idle, expected Off"; return 1; }
    return 0
}
