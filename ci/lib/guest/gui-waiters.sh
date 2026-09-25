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
#
# Sourcing this file does NOT claim the guest driver lock. A long-lived driver
# shell calls qci_claim_driver once, itself, before any broker request. See
# qci_claim_driver at the bottom of this file.
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

# await_dbus_session_name <well-known-name> <user> [timeout] [interval]
# Wait until <well-known-name> HAS AN OWNER on <user>'s session bus.
#
# This is the condition scenarios actually depend on, and for the units these
# scenarios restart it is STRICTLY stronger than `await_user_unit_active`.
# `qstub-notepad.service` and `qdistro-user-relay.service` are plain
# `Type=simple` units, so systemd reports them active the moment the process is
# forked, while the D-Bus name is only claimed later, after the process connects
# to the bus and requests it. (This is a property of THOSE unit types, not of
# systemd in general: a `Type=dbus` or `Type=notify` unit is not reported active
# until its own readiness condition is met.) Everything in between is a
# window in which `is-active` says `active` and a `dbus-send --dest=<name>`
# fails with `org.freedesktop.DBus.Error.ServiceUnknown: The name is not
# activatable` -- these stub services ship no .service activation file, so the
# bus cannot start them on demand and the error is terminal, not retried.
#
# That window is what failed permissions-gui/11 in the 8-way run
# full-20260911T070416Z: S1 `ListReceivers` was missing
# `org.qdistro.StubNotepad.uid3000` while uid 3000's relay-owned receivers were
# all present, and S2-S5 then drove that very stub successfully -- so the defect
# was the readiness gate, not the product.
#
# SCOPE, honestly: for these services name-owned is a LATER MILESTONE in the
# same startup than unit-active -- not a logically stronger predicate, since
# neither implies the other in general (see the converse below) -- and it is
# still weaker than "the method you are about to call will succeed". It asks the BUS DAEMON who
# owns the name, not the service whether it is ready: qstub-notepad claims its
# name before exporting its object and before entering its mainloop
# (stubs/qstub_notepad.py:87,93,100), so a call landing in that gap is QUEUED by
# libdbus rather than answered. Use this as a precondition; keep the scenario's
# real assertion on the method's result. The converse is worth stating too:
# owning a name does NOT imply any particular unit is active -- the owner could
# be a hand-started process. This waiter adds value only for `Type=simple` bus
# services like these stubs; against a `Type=dbus` unit, systemd has already
# gated `restart` on name acquisition, so it would return immediately and prove
# nothing new.
await_dbus_session_name() {
    local name=$1 user=${2:-admin}
    local timeout=${3:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${4:-$QCI_AWAIT_INTERVAL_DEFAULT}
    if [ -z "$name" ]; then
        printf '[await] dbus session name must be non-empty\n' >&2
        return 2
    fi
    _await "dbus session name owned: $name (user $user)" "$timeout" "$interval" \
        _probe_dbus_session_name "$name" "$user"
}
_probe_dbus_session_name() {
    local name=$1 user=$2 uid reply
    uid=$(id -u "$user" 2>/dev/null) || { printf 'no such user: %s' "$user"; return 1; }
    reply=$(runuser -u "$user" -- env \
        XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
        dbus-send --session --print-reply --reply-timeout=5000 \
        --dest=org.freedesktop.DBus /org/freedesktop/DBus \
        org.freedesktop.DBus.GetNameOwner "string:$name" 2>&1) || {
        printf 'owner=<none> %s' "$(printf '%s' "$reply" | tr '\n' ' ')"
        return 1
    }
    # A successful GetNameOwner reply carries the owner's unique name (":1.42").
    printf '%s' "$(printf '%s' "$reply" | tr '\n' ' ')"
    grep -Eq 'string ":[0-9]+\.[0-9]+"' <<<"$reply"
}

# await_dbus_system_name <well-known-name> [timeout] [interval]
# System-bus counterpart of await_dbus_session_name: wait until a service has
# claimed its well-known name on the SYSTEM bus.
#
# Use ONLY for a `Type=simple` system-bus service, where `systemctl restart`
# returns as soon as the process forks and the name is claimed some time later.
# It is NOT needed for a `Type=dbus` unit: systemd marks such a unit started
# only once its BusName is acquired, so restart already blocks on exactly this
# condition. qdistro-admin-broker.service is `Type=dbus` with
# `BusName=org.qdistro.AdminBroker1` (broker/qdistro-admin-broker.service:11-12)
# -- gating on its name adds nothing, and would give a scenario a false sense of
# having closed a race it never had.
await_dbus_system_name() {
    local name=$1 timeout=${2:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${3:-$QCI_AWAIT_INTERVAL_DEFAULT}
    if [ -z "$name" ]; then
        printf '[await] dbus system name must be non-empty\n' >&2
        return 2
    fi
    _await "dbus system name owned: $name" "$timeout" "$interval" \
        _probe_dbus_system_name "$name"
}
_probe_dbus_system_name() {
    local name=$1 reply
    reply=$(dbus-send --system --print-reply --reply-timeout=5000 \
        --dest=org.freedesktop.DBus /org/freedesktop/DBus \
        org.freedesktop.DBus.GetNameOwner "string:$name" 2>&1) || {
        printf 'owner=<none> %s' "$(printf '%s' "$reply" | tr '\n' ' ')"
        return 1
    }
    printf '%s' "$(printf '%s' "$reply" | tr '\n' ' ')"
    grep -Eq 'string ":[0-9]+\.[0-9]+"' <<<"$reply"
}

# await_broker_receiver <uid> <well-known-name> [timeout] [interval]
# Wait until the system broker's ListReceivers exposes <well-known-name> for
# <uid>. ListReceivers is a LIVE query -- the broker fans out to each per-uid
# UserRelay's ListLocalReceivers (broker/qdistro_admin_broker.py:3543-3546),
# which does a live ListNames -- so there is no cached view to lag. What this
# waiter adds over await_dbus_session_name is the RELAY's own readiness: the
# relay is `Type=simple` (user_relay/qdistro-user-relay.service:9) and
# scenarios restart it in the same breath as the stub, so the broker can be
# unable to see a receiver whose own name is already owned.
#
# Assert on THIS when the scenario grades the broker's view
# (permissions-gui/11 S1), and on await_dbus_session_name when it addresses the
# service directly.
await_broker_receiver() {
    local uid=$1 name=$2 timeout=${3:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${4:-$QCI_AWAIT_INTERVAL_DEFAULT}
    if [ -z "$uid" ] || [ -z "$name" ]; then
        printf '[await] broker receiver needs <uid> <name>\n' >&2
        return 2
    fi
    _await "broker receiver visible: $name (uid $uid)" "$timeout" "$interval" \
        _probe_broker_receiver "$uid" "$name"
}
_probe_broker_receiver() {
    local uid=$1 name=$2 reply
    # _await only measures elapsed time BETWEEN probes, so an unbounded
    # dbus-send can overrun the advertised deadline by its own ~25s default.
    reply=$(dbus-send --system --print-reply --reply-timeout=5000 \
        --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.ListReceivers 2>&1) || {
        printf '%s' "$reply"
        return 1
    }
    printf '%s' "$reply"
    # A STRICT record parser, for three reasons found in review:
    #
    #  - FIELD IDENTITY. The signature is a(iss) = (uid, service, friendly)
    #    (broker/qdistro_admin_broker.py:3513,3547-3549). A two-line grep window
    #    after the uid covers BOTH strings, so a receiver whose FRIENDLY LABEL
    #    equalled the service name we want satisfied it: a mocked
    #    (3000, "org.qdistro.DifferentService", "org.qdistro.StubNotepad.uid3000")
    #    matched. Only the FIRST string of a record is the service name.
    #  - SIGPIPE. `grep -A2 ... | grep -Fq` lets the downstream grep exit on the
    #    first hit and SIGPIPE the upstream one; under `set -o pipefail` the
    #    probe then returned 141 on a reply with many structs, so a receiver
    #    that is permanently present timed out. No pipeline here.
    #  - DISCARD IS NOT VALIDATE. The first rewrite still validated a record at
    #    EOF and at the next `struct {`, so an unterminated record matched; and
    #    a nested struct re-synced and its inner triple was graded. Validation
    #    now happens ONLY at a depth-1 `}`; every other path discards.
    #  - CONTAINERS ARE FIELDS. The rewrite after that tracked STRUCT nesting
    #    only, and skipped `array [` / `]` outright. Review reproduced two real
    #    dbus-send replies it wrongly accepted: `aa(iss)` (a matching struct one
    #    array deeper) and `a(issai)` (the three scalars plus a trailing empty
    #    int32 array, which never reached the field counter). Array depth is now
    #    tracked alongside struct depth, and a container opened inside a
    #    candidate counts as a field AND clears `ok`.
    #  - TRACKING TWO CONTAINERS IS NOT TRACKING DEPTH. Tracking `struct {` and
    #    `array [` still left every OTHER container opener invisible, and an
    #    invisible opener is not a wrapper the parser is inside -- it is a line
    #    the parser walks straight through, so the array within it is graded as
    #    the reply's top-level array. Review captured real `dbus-send` output
    #    for `v` -> `(a(iss))` (`variant       struct {`, one line, so the
    #    anchored struct rule never fires) and this file's own author captured
    #    `a{s(iss)}` (`dict entry(`); BOTH were accepted while the contract
    #    below claimed nesting fails closed. The lesson is that an allowlist of
    #    known openers cannot be completed by adding the next one found. So the
    #    parser now rejects on ANY line it does not model -- see the envelope.
    #
    # THE ENVELOPE, which is now enforced rather than assumed. The whole reply
    # must be: an optional `method return ...` header, then EXACTLY ONE
    # top-level `array [ ... ]` -- the header at most ONCE and only BEFORE the
    # array, its suffix unparsed -- containing NOTHING but well-formed `(iss)`
    # records -- each opening at struct depth 0 while array depth is exactly 1,
    # holding EXACTLY three fields whose FULL lines are `int32 <digits>`,
    # `string "..."`, `string "..."` in that order, and closing with `}` at
    # array depth 1. ANY other line anywhere -- a second top-level array, a
    # container this parser does not model, a struct outside the array, an
    # unbalanced `]` or `}`, a SECOND header or one positioned after the array
    # has opened -- sets `bad` and fails the whole reply, as does a single
    # MALFORMED SIBLING record even when a valid match is also present. The one
    # deliberate exception is a BLANK line outside a record, tolerated anywhere
    # because dbus-send's spacing is not worth pinning.
    # At EOF both depths must be zero, so an unterminated container anywhere
    # rejects. This is whole-reply validation, not a search: the match is
    # existential over records that have ALL been validated.
    #
    # RESIDUAL, stated honestly rather than overclaimed: this is still a text
    # parse of dbus-send output, which does not escape string contents. A
    # receiver whose FRIENDLY LABEL contained a newline plus a forged
    # `struct {` block could fabricate a record. That is the only remaining
    # injection surface KNOWN to us -- a claim about our adversarial corpus, not
    # a proof -- and it is unreachable through today's relay: D-Bus well-known
    # names cannot contain a newline, and the relay derives the label from the
    # service name (user_relay/qdistro_user_relay.py:353,608). A typed reader
    # (busctl --json) would close the whole class outright; tracked as
    # follow-up, not done here because it is an untested guest dependency.
    #
    # NOTE on timing: ListReceivers fans out to each relay with its own 5s
    # timeout (broker/qdistro_admin_broker.py:3543), so a relay that owns its
    # name but is not yet dispatching can make the call exceed the 5s
    # --reply-timeout above. The probe then fails and _await retries, and the
    # transcript shows [await] retries that are readiness, not flake. NOTE the
    # limit: retrying only converges if the SLOW relay becomes responsive. An
    # unrelated relay that stays wedged keeps every ListReceivers over 5s and
    # will exhaust this waiter even when the receiver we asked about is healthy.
    # The timeout print (the full reply/error) PRESERVES that evidence but does
    # not attribute it: a generic outer NoReply looks the same for a wedged
    # relay, a stalled broker, another slow operation, or plain scheduling
    # delay. Separating those needs broker logs or a per-relay probe.
    awk -v uid="$uid" -v name="$name" '
        function fieldval(line,   v) {
            v = line
            sub(/^[[:space:]]*string[[:space:]]+"/, "", v)
            sub(/"[[:space:]]*$/, "", v)
            return v
        }
        { sub(/\r$/, "") }
        /^[[:space:]]*array[[:space:]]*\[[[:space:]]*$/ {
            if (sdepth == 1) { nf++; ok = 0 }        # a container IS a field
            else if (sdepth == 0) {
                if (adepth > 0 || seenarray) bad = 1 # only ONE top-level array
                seenarray = 1
            }
            adepth++
            next
        }
        /^[[:space:]]*\][[:space:]]*$/ {
            if (sdepth > 0) ok = 0
            if (adepth > 0) adepth--; else bad = 1
            next
        }
        /^[[:space:]]*struct[[:space:]]*\{[[:space:]]*$/ {
            if (sdepth == 0) {
                nf = 0; opened = (adepth == 1); ok = opened
                if (!opened) bad = 1                 # a struct outside the array
            } else {
                if (sdepth == 1) nf++
                ok = 0
            }
            sdepth++
            next
        }
        /^[[:space:]]*\}[[:space:]]*$/ {
            if (sdepth == 1) {
                if (!(opened && ok && adepth == 1 && nf == 3)) bad = 1
                else if (f1 == uid && fieldval(f2) == name) hit = 1
                opened = 0; ok = 0; nf = 0
            }
            if (sdepth > 0) sdepth--; else bad = 1
            next
        }
        sdepth == 1 {
            nf++
            if (nf == 1) {
                if (NF == 2 && $1 == "int32" && $2 ~ /^[0-9]+$/) f1 = $2; else ok = 0
            } else if (nf == 2) {
                if ($0 ~ /^[[:space:]]*string[[:space:]]+"[^"]*"[[:space:]]*$/) f2 = $0
                else ok = 0
            } else if (nf == 3) {
                if ($0 !~ /^[[:space:]]*string[[:space:]]+"[^"]*"[[:space:]]*$/) ok = 0
            } else ok = 0
            next
        }
        sdepth > 1 { next }                          # already inside a rejected container
        /^[[:space:]]*$/ { next }
        /^method return / {
            # At most ONE, and only BEFORE the array opens -- otherwise a
            # header-prefixed line inside or after the array is a hole in the
            # envelope (codex r6 finding 1). The suffix is opaque: we do not
            # parse sender/serial, only the position of the line.
            if (seenarray || sawheader) bad = 1
            sawheader = 1
            next
        }
        { bad = 1 }                                  # ANY unmodelled token: fail closed
        END { exit !(hit && !bad && adepth == 0 && sdepth == 0) }' <<<"$reply"
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
    reply=$(dbus-send --system --print-reply --reply-timeout=5000 \
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

# --- Backgrounded guest jobs -------------------------------------------------
#
# `wait $(cat /tmp/N.pid)` DOES NOT WAIT when the producer and the consumer are
# separate vm-exec calls, which is the shape every GUI scenario uses:
#
#     vm-exec "$VM" "... & echo \$! >/tmp/N.pid"          # guest shell A
#     vm-exec "$VM" 'wait $(cat /tmp/N.pid); cat /tmp/N.log'   # guest shell B
#
# `wait` only knows its OWN children. In shell B that pid is not a child, so
# bash prints "pid N is not a child of this shell" and returns IMMEDIATELY —
# and the `2>/dev/null` every site carried swallowed the message. The `cat`
# then races the still-running producer and reads an EMPTY or partial log,
# which the scenario reports as a product assertion failure. It was the root
# cause of eight of twelve GUI failures on 2026-09-17, and the scenarios that
# passed that day passed on timing, not on correctness.
#
# A pid file records that a job STARTED. It is not a completion signal. These
# helpers make completion observable the only way it can be: the producer
# itself records its exit status, LAST, and the consumer waits for that record.
#
#     bg_start 45-py1 work '/usr/local/bin/qsu /usr/bin/python3 -c "print(1)"'
#     bg_wait  45-py1 60 || { echo "FAIL: job never finished"; exit 1; }
#     bg_log   45-py1
#     [ "$(bg_rc 45-py1)" = 0 ] || { echo "FAIL: qsu exited $(bg_rc 45-py1)"; exit 1; }
#
# Files live in $QCI_BG_DIR (default /tmp) as <tag>.log, <tag>.rc, <tag>.pid.

: "${QCI_BG_DIR:=/tmp}"

# _bg_base <tag> — echo the path prefix for <tag>, rejecting a tag that would
# escape $QCI_BG_DIR or need shell quoting. Restricting the tag is what lets
# the generated producer script below interpolate these paths safely.
_bg_base() {
    local tag=$1
    case "$tag" in
        ""|*[!A-Za-z0-9._-]*|.*)
            printf '[bg] invalid tag %q: use only [A-Za-z0-9._-], not leading "."\n' \
                "$tag" >&2
            return 1 ;;
    esac
    printf '%s/%s' "$QCI_BG_DIR" "$tag"
}

# bg_start <tag> <user> <command-string>
# Run <command-string> in the background as <user> ("-" for the current user),
# with stdout+stderr in <tag>.log and the exit status in <tag>.rc.
#
# STDIN is /dev/null, explicitly. bash ALREADY does this for an asynchronous
# list in a non-interactive shell, so the redirect changes nothing today and
# no test can tell it from its own absence — it is here so the guarantee
# survives a refactor that stops backgrounding the job, not because it is
# load-bearing now. Several scenarios (48/49/54) spelled `</dev/null` out by
# hand for the same reason.
#
# Set QCI_BG_STDERR=<path> for the one case where stderr must be kept SEPARATE
# from the command's stdout — permissions-gui/54 asserts on the privileged
# command's stdout alone, because a hostile LD_PRELOAD makes the dynamic loader
# warn on stderr before qsu reaches its own sanitization boundary.
#
# The status is written to <tag>.rc.part and then RENAMED into place, so
# <tag>.rc only ever exists complete: a plain `echo $? > <tag>.rc` leaves a
# window between the O_CREAT and the write in which a poller sees a zero-byte
# file and reads an EMPTY status. That window is narrow enough that a
# shell-level poll does not reliably hit it — the bats suite could not make a
# direct-write variant fail — so this is cheap defence in depth, not something
# the tests demonstrate.
#
# Stale files from an earlier step with the same tag are removed FIRST, so a
# bg_wait can never be satisfied by the previous run's record.
bg_start() {
    local tag=$1 user=$2 cmd=$3 base script
    base=$(_bg_base "$tag") || return 2
    rm -f -- "$base.log" "$base.rc" "$base.rc.part" "$base.pid"
    # <command-string> runs inside its own SUBSHELL, and the log redirect is
    # applied to that subshell, for two reasons a plain `%s > log` gets wrong:
    #   - `>` binds to the LAST command of a list, so a multi-command string
    #     would send only its final command's output to the log;
    #   - an `exit N` in the command would otherwise leave the whole background
    #     shell, skipping the exit-status record entirely and hanging bg_wait.
    local errspec='2>&1'
    if [ -n "${QCI_BG_STDERR:-}" ]; then
        printf -v errspec '2> %q' "$QCI_BG_STDERR"
    fi
    # <command-string> is passed through the environment and `eval`ed, NEVER
    # interpolated into the script's source. Interpolation makes the wrapper's
    # own syntax depend on the caller's text: an ordinary fragment ending in a
    # comment (`echo ok  # why`) would comment out the closing `)` and the
    # status record with it, and bg_wait would then block until its deadline
    # for a job that had already finished.
    # The backgrounded GROUP gets its own stdio too, not just the command in
    # it. Otherwise the group process keeps the CALLER's stdout/stderr open for
    # the job's whole lifetime, and qga guest-exec (capture-output) does not
    # report the launching vm-exec finished until every holder of those pipes
    # has closed them: a `bg_start` of a request that waits for an approval
    # then hangs its own vm-exec until the approval that can only come after
    # it (permissions-gui/44 and /46, full-20260922T193137Z-881799).
    printf -v script '{ ( eval "$QCI_BG_CMD" ) > %q %s < /dev/null; echo $? > %q; mv -f %q %q; } < /dev/null > /dev/null 2>&1 & echo $! > %q' \
        "$base.log" "$errspec" "$base.rc.part" "$base.rc.part" "$base.rc" "$base.pid"
    if [ "$user" = "-" ] || [ "$user" = "$(id -un)" ]; then
        QCI_BG_CMD=$cmd bash -c "$script"
    else
        runuser -u "$user" -- env "QCI_BG_CMD=$cmd" bash -c "$script"
    fi
}

# bg_wait <tag> [timeout] [interval] — wait until the job recorded an exit
# status. Waiter contract: the EXIT STATUS is the verdict (0 = the job
# finished, nonzero = it never did within the deadline). The job's OWN exit
# status is a separate question — read it with bg_rc.
bg_wait() {
    local tag=$1 timeout=${2:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${3:-$QCI_AWAIT_INTERVAL_DEFAULT}
    local base
    base=$(_bg_base "$tag") || return 2
    _await "background job to record an exit status: $tag ($base.rc)" \
        "$timeout" "$interval" _probe_bg_done "$base"
}
_probe_bg_done() {
    local base=$1 rc
    if [ ! -f "$base.rc" ]; then
        printf 'no exit status yet (%s.rc absent)' "$base"
        return 1
    fi
    rc=$(cat "$base.rc" 2>/dev/null)
    printf 'exit status recorded: rc=%s' "$rc"
}

# bg_rc <tag> — print the job's recorded exit status on STDOUT. This
# function's own exit status says whether a status could be READ (0 yes,
# 1 no), deliberately NOT the job's status: conflating them would make a job
# that legitimately exited 1 indistinguishable from a job that never ran.
# Compare the stdout, e.g. `[ "$(bg_rc t)" = 0 ]`.
bg_rc() {
    local base rc
    base=$(_bg_base "$1") || return 1
    if [ ! -f "$base.rc" ]; then
        printf '[bg] no exit status recorded for %s (did bg_wait time out?)\n' "$1" >&2
        return 1
    fi
    rc=$(cat "$base.rc" 2>/dev/null)
    printf '%s\n' "$rc"
}

# bg_log <tag> [lines] — print the job's captured output, or its first <lines>
# lines. Absent log (the job never started) is reported loudly rather than as
# empty output, because an empty read is exactly what the pid-wait bug
# produced.
#
# The line limit is an argument rather than a `| head -n` at the call site: a
# pipeline's reader closes early, the writer takes SIGPIPE, and under `set -o
# pipefail` that turns a successful read into a failing step — the same shape
# documented for _await_print_observed above.
bg_log() {
    local base lines
    base=$(_bg_base "$1") || return 1
    if [ ! -f "$base.log" ]; then
        printf '[bg] no log for %s (job never started?)\n' "$1" >&2
        return 1
    fi
    lines=${2:-}
    if [ -n "$lines" ]; then
        head -n "$lines" -- "$base.log"
    else
        cat -- "$base.log"
    fi
}

# --- Guest driver claim ------------------------------------------------------
#
# A GUI scenario driver is one long-lived guest shell. Killing the host-side
# vm-exec that started it does not kill that shell (permissions-gui/08 in
# full-20260924T171310Z-972187, and the solo rerun gui-20260924T193011Z-2597819):
# the agent started another driver, and each leftover shell ran bg_start and
# filed its own broker request. Nothing else in the guest serialises them.
#
# qci_claim_driver [lock-path]
# Take a non-blocking exclusive flock and hold it in THIS shell until it
# exits. Call it once, directly, at the top of the driver — not in a
# subshell, pipeline, command substitution, or `flock -c`. Those release the
# lock as soon as that short-lived process exits, so the rest of the driver
# would run with no claim. A subshell that exits immediately is not the holder.
#
# Pass a scenario-scoped path: /tmp/qci/<slug>/driver.lock. The parent
# directory is created. If the argument is omitted the default is
# /tmp/qci-driver.lock (one lock for the whole guest — prefer the scenario
# path).
#
# Same shell, same path: a second call returns 0 and does not open another
# descriptor. flock(2) is per open file description, so a second open in this
# process would fail against our own lock and look like a second driver.
# That is what lets one driver claim once at the top and source this file
# again afterwards: sourcing never claims, and it must not clear the claim.
# A different path from a shell that already holds a claim is refused without
# releasing the first lock.
#
# Another live holder: print one ERROR line to stderr and `exit 1` the
# calling shell. Do not steal the lock, do not block, and do not arm a
# timeout that would kill the first driver. `exit` (not `return`) so the
# lines after the claim do not run even without `set -e`, and even under
# `qci_claim_driver || true` — exit is not caught by a conditional. There is
# no release helper; the kernel drops the lock when the holding descriptor
# is closed.
#
# Children inherit the descriptor (this bash does not set close-on-exec on
# it). A background child that outlives this shell keeps the claim until that
# child exits too. A fresh vm-exec is not a child of the first driver and
# does not inherit the descriptor.
qci_claim_driver() {
    local lock=${1:-/tmp/qci-driver.lock} dir fd

    if [ "${QCI_DRIVER_CLAIM_DEPTH:-0}" -gt 0 ]; then
        if [ "${QCI_DRIVER_CLAIM_PATH:-}" = "$lock" ]; then
            return 0
        fi
        printf 'ERROR: a second guest driver is already running: %s\n' "$lock" >&2
        exit 1
    fi

    if ! command -v flock >/dev/null 2>&1; then
        printf 'ERROR: qci_claim_driver: flock(1) is required\n' >&2
        exit 2
    fi

    dir=$(dirname -- "$lock")
    if ! mkdir -p -- "$dir" 2>/dev/null; then
        printf 'ERROR: qci_claim_driver: cannot create %s\n' "$dir" >&2
        exit 2
    fi

    # Braces keep the stderr redirect on the open only. A bare
    # `exec REDIR 2>/dev/null` has no command, so BOTH redirections become
    # permanent and the driver shell loses stderr for the rest of its life.
    QCI_DRIVER_CLAIM_FD=""
    if ! { exec {QCI_DRIVER_CLAIM_FD}>>"$lock"; } 2>/dev/null; then
        QCI_DRIVER_CLAIM_FD=""
        printf 'ERROR: qci_claim_driver: cannot open %s\n' "$lock" >&2
        exit 2
    fi
    # -n: fail immediately. No -w. A wait would only delay the second driver,
    # and a timeout that killed the holder would be the opposite of a claim.
    # flock(1) locks the inherited descriptor and exits; this shell keeps that
    # open file description, so the lock outlives the flock process.
    if ! flock -n -x "$QCI_DRIVER_CLAIM_FD" 2>/dev/null; then
        fd=$QCI_DRIVER_CLAIM_FD
        QCI_DRIVER_CLAIM_FD=""
        case $fd in
            ''|*[!0-9]*) ;;
            *) eval "exec ${fd}>&-" 2>/dev/null || true ;;
        esac
        printf 'ERROR: a second guest driver is already running: %s\n' "$lock" >&2
        exit 1
    fi
    QCI_DRIVER_CLAIM_PATH=$lock
    QCI_DRIVER_CLAIM_DEPTH=1
    return 0
}
