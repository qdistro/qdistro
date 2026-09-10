#!/usr/bin/env bash
# qdistro CI GUI scenario waiter library (GUEST-side).
#
# Source this INSIDE the disposable VM to replace the two biggest flake sources
# in GUI scenarios — a fixed `sleep N` before an assertion, and a single-shot
# read of volatile state (`systemctl is-active`, `virsh domstate`, a journal
# grep) — with BOUNDED, OBSERVABLE readiness gates.
#
# Every helper:
#   - polls a small interval up to a bounded deadline (never an unbounded hang);
#   - returns 0 the instant the condition is observed (fast on a quiet host,
#     tolerant on a loaded one — this is what collapses the 8-vs-25 variance);
#   - on TIMEOUT prints, to stderr, the thing it was waiting for, the LAST
#     observed state, and the elapsed seconds, then returns nonzero;
#   - on SUCCESS prints, to stdout, `[await] OK after <n>s: <what>` plus the
#     probe's own observation (`[await] observed: ...`, capped at
#     QCI_AWAIT_OBSERVED_MAX_LINES lines with an ANNOUNCED truncation) — so a
#     step graded by reading the command's output sees the evidence instead of
#     empty stdout. The EXIT STATUS remains the verdict; the print is evidence,
#     not the gate.
#
# STDOUT CONTRACT: grade a waiter by its EXIT STATUS, never by parsing its
# stdout. A caller that must capture a probe's payload for machine parsing sets
# QCI_AWAIT_QUIET=1 to suppress the success announcement — never to make a
# failure quieter (timeouts still go to stderr regardless). Set it PER CALL
# (`QCI_AWAIT_QUIET=1 await_file ...`); exporting it silences every waiter's
# success evidence in the whole process, which is how a passing gate became
# invisible in the first place.
#
# CRITICAL — this is hardening, NOT masking: a waiter only rides out
# nondeterministic READINESS. It must wait for the SAME condition the assertion
# checks, with a bounded deadline, and fail LOUD when the condition never holds.
# Never widen a waiter to swallow a real product failure (e.g. do not `|| true`
# a waiter, and do not wait on a weaker condition than the one you assert).
#
# Delivery: the host copies this file into the VM at /tmp/qci-gui-waiters.sh
# (see install_gui_waiters in ci/lib/gates/gui.sh); markdown scenarios source
# that path. Host-side driver scripts can source the repo copy directly. The
# library has NO host-only dependencies.
# shellcheck shell=bash

# Defaults (override per call). Deadlines are intentionally modest: a waiter is a
# readiness gate, not a licence to hang. Scenarios that legitimately need longer
# pass an explicit timeout argument.
: "${QCI_AWAIT_TIMEOUT_DEFAULT:=30}"
: "${QCI_AWAIT_INTERVAL_DEFAULT:=1}"

# _await <description> <timeout_s> <interval_s> <probe-cmd...>
# Core poll loop. Runs <probe-cmd> until it exits 0 or <timeout_s> elapses. The
# probe's combined stdout+stderr from the final attempt is reported as the "last
# observed state" on timeout, so a probe that echoes the value it saw produces a
# self-explaining failure. Bounded by the wall clock via SECONDS. Returns 0 when
# ready, 1 on timeout.
# _await_print_observed <text> — echo a probe's success observation, bounded so
# a chatty probe (e.g. a whole dbus reply) cannot bury a scenario transcript.
# Two independent caps apply, in order:
#   QCI_AWAIT_OBSERVED_MAX_LINES (default 20)  — line count
#   QCI_AWAIT_OBSERVED_MAX_BYTES (default 8192) — total bytes, which is the cap
#     that actually protects the qga guest-exec capture path, since ONE long
#     line defeats a line cap entirely.
# Truncation is ANNOUNCED, never silent, and only ever applies to the SUCCESS
# path — a timeout still reports the last observed state in full.
#
# NO PIPELINES HERE, DELIBERATELY. The obvious `printf ... | head -n N` closes
# the read end early, so printf takes SIGPIPE and the command substitution
# yields 141. Under `set -euo pipefail` — which scenarios run with — pipefail
# propagates that 141 out of this function and CONVERTS A SUCCESSFUL WAIT INTO A
# FAILURE. That is precisely the bug class this whole file exists to remove, so
# the line cap is applied with mapfile over a here-string instead.
#
# SCOPE: this claim is about _await_print_observed ONLY. Several _probe_*
# helpers below still use early-closing pipelines (`| head -n1`, `| grep -m1`).
# They are safe AS CALLED, because _await evaluates them in an `if` condition
# and decides success from the captured value; but calling a _probe_* directly
# under `set -e -o pipefail` is NOT safe. Fix those before using one standalone.
: "${QCI_AWAIT_OBSERVED_MAX_LINES:=20}"
: "${QCI_AWAIT_OBSERVED_MAX_BYTES:=8192}"

# _await_positive_int <value> <fallback> — echo <value> canonicalized as a
# positive base-10 integer, else <fallback>. A malformed cap must never abort a
# passing waiter, so every rejection falls back rather than failing.
#
# Digit-only is NOT sufficient validation, because the callers use these values
# in arithmetic and array-slice contexts:
#   - "08" is digit-only but reads as OCTAL in $(( )), so it errors with
#     "value too great for base" and, under set -e, kills a successful waiter.
#   - "000" is digit-only and positive-looking but is zero.
#   - a 30-digit string overflows a 64-bit shell integer ("integer expected").
# Hence: bound the length first, then force base 10, then require >= 1.
_await_positive_int() {
    local v=$1 fallback=$2 n
    case $v in
        '' | *[!0-9]* ) printf '%s\n' "$fallback"; return 0 ;;
    esac
    # Bound BEFORE any arithmetic: bash integers are 64-bit, and an overlong
    # literal makes $(( )) fail rather than wrap.
    if [ "${#v}" -gt 18 ]; then printf '%s\n' "$fallback"; return 0; fi
    # 10# forces base 10, so a leading zero is a leading zero, not octal.
    n=$((10#$v))
    if [ "$n" -lt 1 ]; then printf '%s\n' "$fallback"; return 0; fi
    printf '%s\n' "$n"
}

_await_print_observed() {
    # Byte-scoped for the whole function: under LC_ALL=C, ${#s} and ${s:0:n}
    # count bytes rather than characters, which is what the qga stream cap
    # measures. `local` restores the caller's locale on return.
    local LC_ALL=C
    local text=$1 max_lines max_bytes total shown dropped_lines dropped_bytes
    local -a lines

    max_lines=$(_await_positive_int "${QCI_AWAIT_OBSERVED_MAX_LINES}" 20)
    max_bytes=$(_await_positive_int "${QCI_AWAIT_OBSERVED_MAX_BYTES}" 8192)

    mapfile -t lines <<< "$text"
    total=${#lines[@]}

    if [ "$total" -le "$max_lines" ]; then
        shown=$text
        dropped_lines=0
    else
        printf -v shown '%s\n' "${lines[@]:0:$max_lines}"
        shown=${shown%$'\n'}
        dropped_lines=$((total - max_lines))
    fi

    dropped_bytes=0
    if [ "${#shown}" -gt "$max_bytes" ]; then
        dropped_bytes=$((${#shown} - max_bytes))
        shown=${shown:0:$max_bytes}
    fi

    printf '[await] observed: %s\n' "$shown"
    if [ "$dropped_lines" -gt 0 ]; then
        printf '[await] observed: ... (truncated, %s more line(s))\n' "$dropped_lines"
    fi
    if [ "$dropped_bytes" -gt 0 ]; then
        printf '[await] observed: ... (truncated, %s more byte(s))\n' "$dropped_bytes"
    fi
    return 0
}

_await() {
    local desc=$1 timeout=$2 interval=$3; shift 3
    local start=$SECONDS last="" elapsed
    while :; do
        if last=$("$@" 2>&1); then
            # Report the SUCCESS as loudly as the timeout. A waiter that
            # returned 0 in silence is indistinguishable, on stdout, from a
            # waiter that never ran — and a scenario (or an agent driving one)
            # that grades the step by grepping the command's output then reads
            # the empty stdout as a FAILURE even though the condition held.
            # That is exactly how permissions-gui/59 S2 failed while the broker
            # had in fact logged `lineage_enforce=True` 1s after the restart.
            # Printing the probe's own observation makes the evidence visible
            # without weakening anything: it is emitted ONLY on the path where
            # the probe already exited 0.
            elapsed=$((SECONDS - start))
            if [ "${QCI_AWAIT_QUIET:-0}" != 1 ]; then
                printf '[await] OK after %ss: %s\n' "$elapsed" "$desc"
                if [ -n "$last" ]; then
                    _await_print_observed "$last"
                fi
            fi
            return 0
        fi
        elapsed=$((SECONDS - start))
        if [ "$elapsed" -ge "$timeout" ]; then
            printf '[await] TIMEOUT after %ss waiting for: %s\n' "$elapsed" "$desc" >&2
            if [ -n "$last" ]; then
                printf '[await] last observed: %s\n' "$last" >&2
            else
                printf '[await] last observed: (condition never true; no probe output)\n' >&2
            fi
            return 1
        fi
        sleep "$interval"
    done
}

# await_file <path> [timeout] [interval] — wait until <path> exists.
await_file() {
    local path=$1 timeout=${2:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${3:-$QCI_AWAIT_INTERVAL_DEFAULT}
    _await "file to exist: $path" "$timeout" "$interval" test -e "$path"
}

# await_socket <path> [timeout] [interval] — wait until <path> is a socket.
await_socket() {
    local path=$1 timeout=${2:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${3:-$QCI_AWAIT_INTERVAL_DEFAULT}
    _await "socket to exist: $path" "$timeout" "$interval" test -S "$path"
}

# await_x11_window <title-pattern> [user] [display] [timeout] [interval]
# Wait until xdotool can resolve a visible X11/XWayland window whose title
# matches <title-pattern>. This is intentionally bounded: `xdotool search
# --sync` can wait forever when an application crashes before mapping, hiding
# the useful application log behind an agent-level timeout.
await_x11_window() {
    local title=$1 user=${2:-admin} display=${3:-:0}
    local timeout=${4:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${5:-$QCI_AWAIT_INTERVAL_DEFAULT}
    _await "visible X11 window matching: $title (user $user, display $display)" \
        "$timeout" "$interval" _probe_x11_window "$title" "$user" "$display"
}
_probe_x11_window() {
    local title=$1 user=$2 display=$3 wid
    wid=$(runuser -u "$user" -- env DISPLAY="$display" \
        xdotool search --onlyvisible --name "$title" 2>&1 | head -n1)
    printf 'window_id=%s' "${wid:-<none>}"
    [[ "$wid" =~ ^[0-9]+$ ]]
}

# await_user_unit_active <unit> [user] [timeout] [interval]
# Wait until a per-user systemd unit reports `active`, queried AS the session
# user with XDG_RUNTIME_DIR set (so a root caller still reaches the user manager).
await_user_unit_active() {
    local unit=$1 user=${2:-admin} timeout=${3:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${4:-$QCI_AWAIT_INTERVAL_DEFAULT}
    _await "user unit active: $unit (user $user)" "$timeout" "$interval" \
        _probe_user_unit_active "$unit" "$user"
}
_probe_user_unit_active() {
    local unit=$1 user=$2 uid state
    uid=$(id -u "$user" 2>/dev/null) || { printf 'no such user: %s' "$user"; return 1; }
    state=$(runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" \
        systemctl --user is-active "$unit" 2>/dev/null)
    printf 'state=%s' "${state:-unknown}"
    [ "$state" = active ]
}

# await_system_unit_active <unit> [timeout] [interval]
# System-scope counterpart of await_user_unit_active: wait until a SYSTEM systemd
# unit reports `active` (`systemctl is-active <unit>`, no --user). Use for
# preconditions on system services (e.g. a broker socket unit) that a fresh VM
# may still be starting when the scenario begins.
await_system_unit_active() {
    local unit=$1 timeout=${2:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${3:-$QCI_AWAIT_INTERVAL_DEFAULT}
    _await "system unit active: $unit" "$timeout" "$interval" \
        _probe_system_unit_active "$unit"
}

# await_broker_pending_action <action> [timeout] [interval]
# Wait until GetPending exposes the exact action created by the operation under
# test.  Printing the full reply on timeout keeps a missing request distinct
# from a request that was created with the wrong target/action.
await_broker_pending_action() {
    local action=$1 timeout=${2:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${3:-$QCI_AWAIT_INTERVAL_DEFAULT}
    if [ -z "$action" ]; then
        printf '[await] broker pending action must be non-empty\n' >&2
        return 2
    fi
    _await "broker pending action: $action" "$timeout" "$interval" \
        _probe_broker_pending_action "$action"
}
_probe_broker_pending_action() {
    local action=$1 reply
    reply=$(dbus-send --system --print-reply \
        --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.GetPending 2>&1) || {
        printf '%s' "$reply"
        return 1
    }
    printf '%s' "$reply"
    grep -Fq "string \"$action\"" <<<"$reply"
}
_probe_system_unit_active() {
    local unit=$1 state
    state=$(systemctl is-active "$unit" 2>/dev/null)
    printf 'state=%s' "${state:-unknown}"
    [ "$state" = active ]
}

# await_journal_line_after_cursor <cursor> <ere-pattern> [timeout] [interval] [journalctl-args...]
# Wait for a journal line matching <ere-pattern> that appears AFTER <cursor>.
# Capture the cursor BEFORE the action you are about to drive, e.g.:
#   cur=$(journalctl --user -n0 --show-cursor 2>/dev/null | sed -n 's/^-- cursor: //p')
# then drive the action, then await the event. Scoping by cursor is what makes
# this sound: a stale line emitted BEFORE the action can never satisfy the wait,
# so the gate proves the action's OWN effect, not a leftover. Extra journalctl
# args (e.g. --user, -u qdwin-compositor.service) are forwarded.
await_journal_line_after_cursor() {
    local cursor=$1 pattern=$2 timeout=${3:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${4:-$QCI_AWAIT_INTERVAL_DEFAULT}
    shift 4 2>/dev/null || shift "$#"
    _await "journal line /$pattern/ after cursor" "$timeout" "$interval" \
        _probe_journal_after_cursor "$cursor" "$pattern" "$@"
}
_probe_journal_after_cursor() {
    local cursor=$1 pattern=$2; shift 2
    local hit
    hit=$(journalctl "$@" --after-cursor "$cursor" --no-pager -o cat 2>/dev/null \
        | grep -E -m1 "$pattern")
    if [ -n "$hit" ]; then
        printf 'matched: %s' "$hit"
        return 0
    fi
    printf 'no line matching /%s/ since cursor yet' "$pattern"
    return 1
}

# await_window_mapped <app_id> [timeout] [interval] [journalctl-args...]
# Wait until the compositor journal reports a toplevel mapped for <app_id>
# (`toplevel_added app_id=<app_id>`). Defaults to the user journal; pass
# -u qdwin-compositor.service (etc.) as trailing args to scope to a unit.
await_window_mapped() {
    local app_id=$1 timeout=${2:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${3:-$QCI_AWAIT_INTERVAL_DEFAULT}
    shift 3 2>/dev/null || shift "$#"
    local args=("$@"); [ "${#args[@]}" -gt 0 ] || args=(--user)
    _await "window mapped: app_id=$app_id" "$timeout" "$interval" \
        _probe_window_mapped "$app_id" "${args[@]}"
}
_probe_window_mapped() {
    local app_id=$1; shift
    local hit
    hit=$(journalctl "$@" --no-pager -o cat 2>/dev/null \
        | grep -E -m1 "toplevel_added.*app_id=${app_id}([^a-zA-Z0-9_-]|$)")
    if [ -n "$hit" ]; then printf 'matched: %s' "$hit"; return 0; fi
    printf 'no toplevel_added for app_id=%s yet' "$app_id"
    return 1
}

# await_domstate <domain> <expected-state> [timeout] [interval]
# Wait until `virsh domstate <domain>` equals <expected-state> (e.g. running,
# "shut off"). For NESTED guests this runs inside the disposable VM, which is the
# libvirt host for its tier-4/5 child. Tolerant of a transient empty/error read
# (the documented tier5 single-shot domstate flake) — it keeps polling rather
# than treating one bad read as the verdict.
await_domstate() {
    local dom=$1 want=$2 timeout=${3:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${4:-$QCI_AWAIT_INTERVAL_DEFAULT}
    _await "domstate of $dom == '$want'" "$timeout" "$interval" \
        _probe_domstate "$dom" "$want"
}
_probe_domstate() {
    local dom=$1 want=$2 state
    state=$(virsh domstate "$dom" 2>/dev/null | tr -d '\r' | head -n1)
    printf 'domstate=%s' "${state:-<empty>}"
    [ "$state" = "$want" ]
}

# await_domain_gone <domain> [timeout] [interval]
# Wait until <domain> is REAPED — either undefined/absent (domstate errors →
# empty read) or in a TERMINAL stopped state ("shut off", crashed). This is the
# reap-verification counterpart of await_domstate: after driving a window/VM
# close, a one-shot domstate can still catch the guest mid-teardown; poll until
# it is genuinely gone. Deliberately keeps waiting on live/transitional states
# (running, paused, pmsuspended, blocked, "in shutdown") — those do NOT prove the
# domain was reaped, so accepting them would mask an incomplete teardown.
await_domain_gone() {
    local dom=$1 timeout=${2:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${3:-$QCI_AWAIT_INTERVAL_DEFAULT}
    _await "domain reaped (absent or terminally stopped): $dom" "$timeout" "$interval" \
        _probe_domain_gone "$dom"
}
_probe_domain_gone() {
    local dom=$1 state
    state=$(virsh domstate "$dom" 2>/dev/null | tr -d '\r' | head -n1)
    printf 'domstate=%s' "${state:-<absent>}"
    case "$state" in
        ""|"shut off"|crashed) return 0 ;;
        *) return 1 ;;
    esac
}
