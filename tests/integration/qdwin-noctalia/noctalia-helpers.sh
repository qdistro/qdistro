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
# A fixed one-second sleep raced journal visibility under parallel GUI load.
: "${NOCT_CURSOR_WAIT_S:=10}"
: "${NOCT_CURSOR_POLL_S:=0.25}"

# Count `mapped on cursor_layer` remaps whose nonzero_alpha token is not 0.
# Reads a journal excerpt on stdin. nonzero_alpha=10 counts; nonzero_alpha=0,
# a negative value, and a line with no nonzero_alpha token do not. This is the
# predicate assert 1.1/2.1/3.1 in 04-cursor-tracking.md checks.
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
    local cur="$1"
    noct_compositor_journal_after "$cur" | noct_count_cursor_layer_nonzero_alpha
}

# Poll cursor_layer_nonzero_alpha_after until it is >= 1 or the deadline
# passes. $1 is the journal cursor; $2 overrides NOCT_CURSOR_WAIT_S (seconds).
# Returns 0 on the first sighting. On expiry prints FAIL (bound, cursor, last
# count, journal tail) and returns 1. Each iteration re-reads the journal.
noct_wait_cursor_layer_nonzero_alpha() {
    local cur="$1"
    local timeout="${2:-$NOCT_CURSOR_WAIT_S}"
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
        count=$(printf '%s\n' "$text" | noct_count_cursor_layer_nonzero_alpha)
        case "$count" in
            ''|*[!0-9]*) count=0 ;;
        esac
        if [ "$count" -ge 1 ]; then
            return 0
        fi
        now_ms=$(date +%s%3N) || now_ms=$start_ms
        elapsed_ms=$(( now_ms - start_ms ))
        if [ "$elapsed_ms" -ge "$limit_ms" ]; then
            echo "FAIL: timed out after ${timeout}s waiting for mapped on cursor_layer with nonzero_alpha>0 (elapsed_ms=${elapsed_ms} cursor=${cur} last_count=${count})"
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
