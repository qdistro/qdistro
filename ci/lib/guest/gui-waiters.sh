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
# broker_pending_count
# Print ONE line: the number of undecided requests GetPending returns (`0`
# when none). Use it instead of parsing dbus-send's pretty-printed array:
# `--print-reply` spreads an empty array over an indented `array [` / `]`
# pair under a `method return` header, and hand-written matchers of that
# layout keep breaking (permissions-gui/25, full-20261001T124446Z-1395361:
# `grep '^]$'` missed the indented `]` and the run went ERROR on a correct
# broker). busctl prints the signature and the element count first
# (`aa{sv} 0`), so the count is exact. The reply is checked to be that
# signature; anything else (an error, a changed method) prints the raw reply
# to stderr and returns 1, never a count. Compare the stdout of this call
# alone, e.g. `n=$(broker_pending_count) && [ "$n" = 0 ]`.
broker_pending_count() {
    local reply sig n
    # stdout only: busctl's diagnostics go straight to the caller's stderr
    # and can never be read as part of the reply.
    if ! reply=$(busctl --system call org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1 GetPending); then
        printf 'ERROR: broker_pending_count: GetPending failed: %s\n' "$reply" >&2
        return 1
    fi
    read -r sig n _ <<<"$reply"
    case $n in ''|*[!0-9]*) sig= ;; esac
    if [ "$sig" != 'aa{sv}' ]; then
        printf 'ERROR: broker_pending_count: unexpected GetPending reply: %s\n' "$reply" >&2
        return 1
    fi
    printf '%s\n' "$n"
}
# qci_sqlite <audit|approvals|/abs/path.sqlite> <sql>
# Run ONE SQL text against a broker database and print sqlite3's output. The
# SQL travels as a single argv word straight to sqlite3: it is never a printf
# format, never echoed, never re-expanded. Scenarios write it single-quoted
# in the guest, e.g.
#   qci_sqlite audit "SELECT decision, scope FROM audit WHERE action LIKE 'app.send-to:%' ORDER BY id DESC LIMIT 1;"
# and the guest driver copies that line VERBATIM. Rewriting it as
# `printf "...LIKE 'x:%'..." | sqlite3` made `%'` a printf directive, so the
# query never ran and a correct product run went ERROR
# (permissions-gui/14, full-20261006T224555Z-2478214). The exit status is
# sqlite3's; a missing argument or unknown alias returns 2.
qci_sqlite() {
    local db=${1-} sql=${2-}
    case $db in
        audit) db=/var/lib/qdistro/audit/audit.sqlite ;;
        approvals) db=/var/lib/qdistro/approvals/approvals.sqlite ;;
        /*) ;;
        *) printf 'ERROR: qci_sqlite: unknown database %s (audit|approvals|/abs/path)\n' "$db" >&2
           return 2 ;;
    esac
    if [ "$#" -ne 2 ] || [ -z "$sql" ]; then
        printf 'ERROR: qci_sqlite: usage: qci_sqlite <audit|approvals|/abs/path> <sql>\n' >&2
        return 2
    fi
    sqlite3 -batch "$db" "$sql"
}
# ---- qdwin lane: windows by title, focus, idle locker ----------------------
#
# The GUI scenarios run the product session (qdwin compositor + qdshell).
# These helpers are its window primitives. Everything comes from what the compositor itself records:
#
#   qdwin: toplevel_added handle=H uid=U pid=P app_id=A title="T"
#   qdwin: toplevel_title handle=H title="T"     (each later change)
#   qdwin: toplevel_removed handle=H
#   qdwin: focus handle=H (was P) seat=S          (keyboard focus moves)
#
# read from the journal of the RUNNING compositor process only (its MainPID),
# so handles a restarted compositor reused can never be confused. A window's
# current title is its add-time title overridden by its latest
# toplevel_title line. Run as root (the journal reader), like every helper here.

# qdwin_compositor_pid — MainPID of admin's qdwin-compositor.service, or fail.
qdwin_compositor_pid() {
    local pid
    pid=$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        systemctl --user show qdwin-compositor.service -p MainPID --value 2>/dev/null)
    case "$pid" in ''|0|*[!0-9]*)
        printf 'ERROR: qdwin-compositor.service has no running MainPID (got %s)\n' "${pid:-nothing}" >&2
        return 1 ;;
    esac
    printf '%s\n' "$pid"
}

# qdwin_windows — one line per LIVE toplevel of the running compositor:
#   <handle>\t<pid>\t<uid>\t<app_id>\t<title>
# in handle order. Fails when the compositor or its journal is unavailable.
qdwin_windows() {
    local pid journal
    pid=$(qdwin_compositor_pid) || return 1
    # An unreadable journal is an observation error, not an empty window
    # list. In particular the dialog probe must not treat it as dismissal.
    journal=$(journalctl _PID="$pid" --no-pager -o cat 2>/dev/null) || {
        printf 'ERROR: qdwin_windows: cannot read compositor journal for pid %s\n' "$pid" >&2
        return 1
    }
    printf '%s\n' "$journal" | awk '
        function after(s, key,   i) { i = index(s, key); return i ? substr(s, i + length(key)) : "" }
        function unquote(s) { sub(/^"/, "", s); sub(/"$/, "", s); return s }
        /qdwin: toplevel_added handle=[0-9]+ / {
            rest = after($0, "qdwin: toplevel_added ")
            split(rest, f, " ")
            h = after(f[1], "handle="); u = after(f[2], "uid="); p = after(f[3], "pid=")
            a = after(f[4], "app_id=")
            t = index(rest, " title=\"") ? unquote(after(rest, " title=")) : ""
            live[h] = 1; P[h] = p; U[h] = u; A[h] = a; T[h] = t
            next
        }
        /qdwin: toplevel_title handle=[0-9]+ / {
            rest = after($0, "qdwin: toplevel_title ")
            h = after(substr(rest, 1, index(rest, " ") - 1), "handle=")
            if (h in live) T[h] = unquote(after(rest, " title="))
            next
        }
        /qdwin: toplevel_removed handle=[0-9]+/ {
            h = after($0, "qdwin: toplevel_removed handle="); sub(/[^0-9].*$/, "", h)
            delete live[h]
            next
        }
        END { for (h in live) printf "%s\t%s\t%s\t%s\t%s\n", h, P[h], U[h], A[h], T[h] }
    ' | sort -n
}

# qdwin_window_handle <title-ere> [pid] — the handle of the NEWEST live
# toplevel whose current title matches <title-ere> as a WHOLE string,
# optionally only one owned by
# client <pid>. Prints nothing and fails when there is none.
qdwin_window_handle() {
    local pattern=$1 want_pid=${2:-} h p u a t found=""
    [ -n "$pattern" ] || { printf 'qdwin_window_handle: needs a title pattern\n' >&2; return 2; }
    while IFS=$'\t' read -r h p u a t; do
        [ -n "$h" ] || continue
        [ -z "$want_pid" ] || [ "$p" = "$want_pid" ] || continue
        if printf '%s\n' "$t" | grep -Exq -- "$pattern"; then found=$h; fi
    done < <(qdwin_windows)
    [ -n "$found" ] || return 1
    printf '%s\n' "$found"
}

# await_qdwin_window_title <title-ere> [timeout] [interval] [pid]
# Wait until a live toplevel's current title matches <title-ere> as a whole string. The admin approvals
# window titles itself `admin approvals (N pending)` from the rows its Pending
# list displays, and qdwin logs a title change on the commit that carries it,
# so this waits for the app's state instead of a sleep. On timeout every live
# window's title is printed.
await_qdwin_window_title() {
    local pattern=$1 timeout=${2:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${3:-$QCI_AWAIT_INTERVAL_DEFAULT} want_pid=${4:-}
    [ -n "$pattern" ] || { printf '[await] qdwin window title needs a title pattern\n' >&2; return 2; }
    _await "qdwin window title /$pattern/${want_pid:+ (pid $want_pid)}" "$timeout" "$interval" \
        _probe_qdwin_window_title "$pattern" "$want_pid"
}
_probe_qdwin_window_title() {
    local pattern=$1 want_pid=$2 h titles
    titles=$(qdwin_windows 2>&1 | awk -F'\t' '{printf "%s%s=%s", (NR>1?"|":""), $1, $5}')
    if h=$(qdwin_window_handle "$pattern" "$want_pid"); then
        printf 'handle=%s titles=[%s]' "$h" "$titles"
        return 0
    fi
    printf 'titles=[%s]' "$titles"
    return 1
}

# qdwin_focused_handle — the handle that holds keyboard focus now (the last
# `qdwin: focus` line of the running compositor), 4294967295 for none.
qdwin_focused_handle() {
    local pid h
    pid=$(qdwin_compositor_pid) || return 1
    h=$(journalctl _PID="$pid" --no-pager -o cat 2>/dev/null \
        | sed -n 's/.*qdwin: focus handle=\([0-9][0-9]*\) (was .*/\1/p' | tail -n1)
    printf '%s\n' "${h:-4294967295}"
}

# qdwin_focus_window <title-ere> [timeout] [pid]
# Find the newest live window whose title matches, ask qdshell to focus it (the same
# `qs ipc call qdwin focusWindow` the taskbar uses), and return only once the
# compositor reports keyboard focus on that handle. Prints the handle.
# Fails, naming what it saw, when no window matches or focus never lands.
qdwin_focus_window() {
    local pattern=$1 timeout=${2:-15} want_pid=${3:-} h start=$SECONDS f
    h=$(QCI_AWAIT_QUIET=1 await_qdwin_window_title "$pattern" "$timeout" 0.5 "$want_pid" >/dev/null \
        && qdwin_window_handle "$pattern" "$want_pid") \
        || { printf 'ERROR: qdwin_focus_window: no live window titled /%s/\n' "$pattern" >&2; return 1; }
    while :; do
        f=$(qdwin_focused_handle) || return 1
        if [ "$f" = "$h" ]; then
            printf '%s\n' "$h"
            return 0
        fi
        if [ $((SECONDS - start)) -ge "$timeout" ]; then
            printf 'ERROR: qdwin_focus_window: focus stayed on handle %s, not %s (/%s/)\n' "$f" "$h" "$pattern" >&2
            return 1
        fi
        runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
            qs ipc -p /usr/share/quickshell/qdshell call qdwin focusWindow "$h" >/dev/null 2>&1 || true
        sleep 0.5
    done
}

# qdwin_guard_idle_locker — keep qdlocker from taking the screen and the
# keyboard mid-scenario, then PROVE it is unlocked (permissions-gui/18's
# guard). The gate's suppress_idle_lock already installs the idle drop-in; this
# also enables the root-owned status introspection marker, restarts the locker
# so both take effect, and fails closed unless it answers locked=False.
qdwin_guard_idle_locker() {
    local d=/home/admin/.config/systemd/user/qdlocker.service.d reply i
    faillock --user admin --reset 2>/dev/null || true
    install -d -m 0755 -o 0 -g 0 /etc/qdistro || return 1
    : > /etc/qdistro/locker-ctrl-introspection || return 1
    chown 0:0 /etc/qdistro/locker-ctrl-introspection
    chmod 0644 /etc/qdistro/locker-ctrl-introspection
    install -d -m 0755 -o admin -g users "$d" || return 1
    printf '[Service]\nEnvironment=QDLOCKER_IDLE_MS=86400000\n' > "$d/90-ci-gui.conf" || return 1
    chown admin:users "$d/90-ci-gui.conf"
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload || return 1
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service || return 1
    reply=""
    for i in $(seq 1 20); do
        reply=$(runuser -u admin -- bash -c "printf 'status\n' | socat -t 1 - UNIX-CONNECT:/run/user/1000/qdlocker.sock" 2>/dev/null) || reply=""
        case "$reply" in *locked=*) break ;; esac
        sleep 0.5
    done
    case "$reply" in
        *locked=False*) printf 'qdlocker unlocked (idle lock suppressed)\n'; return 0 ;;
        *locked=True*) printf 'ERROR: qdlocker is locked; input would go to the lock overlay\n' >&2; return 1 ;;
        *) printf 'ERROR: qdlocker status introspection unavailable (%s); cannot prove input is unobstructed\n' "${reply:-no reply}" >&2; return 1 ;;
    esac
}

# ---- qdwin lane: the admin approvals app -----------------------------------
#
# The permissions-gui admin-app scenarios that run on the qdwin lane drive the
# SHIPPED app the way the image launches it: native Wayland, through
# /usr/local/bin/qdistro-start-admin-app (deploy/start-admin-app-wayland.sh).

# qdwin_admin_lane_setup [--silos] — preconditions for an admin-app scenario
# on the qdwin lane: the product session is up, the idle locker is held off
# (qdwin_guard_idle_locker), and, with --silos, the work (uid 2000) / work2
# (uid 3000) silo fixtures exist. The qdwin golden has no silos on purpose
# (scripts/vm/lib/gui-silo-fixtures.sh says why), so the scenarios that file
# requests as work/work2 provision them here, on their own disposable VM.
qdwin_admin_lane_setup() {
    local silos=0
    case "${1:-}" in
        "") ;;
        --silos) silos=1 ;;
        *) printf 'usage: qdwin_admin_lane_setup [--silos]\n' >&2; return 2 ;;
    esac
    await_socket /run/user/1000/wayland-1 60 >/dev/null || return 1
    await_user_unit_active qdwin-compositor.service admin 60 >/dev/null || return 1
    await_user_unit_active qdshell.service admin 60 >/dev/null || return 1
    systemctl is-active --quiet qdistro-admin-broker.service \
        || systemctl start qdistro-admin-broker.service || return 1
    grep -qx 'export QT_QPA_PLATFORM=wayland' /usr/local/bin/qdistro-start-admin-app \
        || { printf 'ERROR: /usr/local/bin/qdistro-start-admin-app is not the shipped Wayland launcher\n' >&2; return 1; }
    if [ "$silos" = 1 ]; then
        bash /root/qdistro-src/scripts/vm/lib/gui-silo-fixtures.sh || return 1
    fi
    qdwin_guard_idle_locker
}

# qdwin_start_admin_app [app args...] — start the admin app through the shipped
# launcher in its first-paint mode and print the app's pid. Returns only once
# the window has painted its first frame and the compositor holds it (see the
# launcher), so a capture right after it is not a bare desktop or a half-drawn
# window; fails (with the launcher's account) otherwise.
qdwin_start_admin_app() {
    local out rc=0 pid
    out=$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
        DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
        QDISTRO_ADMIN_APP_WAIT_PAINTED=1 /usr/local/bin/qdistro-start-admin-app "$@") || rc=$?
    pid=$(printf '%s\n' "$out" | sed -n '/^[0-9][0-9]*$/p' | tail -n1)
    if [ "$rc" -ne 0 ] || [ -z "$pid" ]; then
        printf 'ERROR: qdistro-start-admin-app exited %s (pid %s); see the launcher output above and the app log it named\n' \
            "$rc" "${pid:-none}" >&2
        return 1
    fi
    # The launcher proved the APP painted its first frame (the app's marker).
    # Qt's sync() is a no-op on Wayland, so the app cannot prove the
    # COMPOSITOR processed that frame; qdwin can: it logs `mapped handle=H`
    # while processing the first commit that carries a buffer, and Qt's first
    # buffer is that painted frame. Wait for that line for THIS pid.
    QCI_AWAIT_QUIET=1 await_qdwin_window_mapped_pid "$pid" 30 0.2 >/dev/null || return 1
    printf '%s\n' "$pid"
}

# await_qdwin_window_mapped_pid <pid> [timeout] [interval] — wait until the
# running compositor has mapped a toplevel of client <pid> (its
# `toplevel_added ... pid=<pid>` handle has a `mapped handle=` line), i.e. it
# has processed a commit carrying that window's first buffer.
await_qdwin_window_mapped_pid() {
    local pid=$1 timeout=${2:-$QCI_AWAIT_TIMEOUT_DEFAULT} interval=${3:-$QCI_AWAIT_INTERVAL_DEFAULT}
    _await "qdwin mapped a window of pid $pid" "$timeout" "$interval" \
        _probe_qdwin_window_mapped_pid "$pid"
}
_probe_qdwin_window_mapped_pid() {
    local want=$1 cpid
    cpid=$(qdwin_compositor_pid) || return 1
    journalctl _PID="$cpid" --no-pager -o cat 2>/dev/null | awk -v want="$want" '
        /qdwin: toplevel_added handle=[0-9]+ uid=[0-9]+ pid=/ {
            h = $0; sub(/.*qdwin: toplevel_added handle=/, "", h); sub(/ .*/, "", h)
            p = $0; sub(/.*qdwin: toplevel_added handle=[0-9]+ uid=[0-9]+ pid=/, "", p); sub(/ .*/, "", p)
            if (p == want) mine[h] = 1
            next
        }
        /qdwin: mapped handle=[0-9]+ / {
            h = $0; sub(/.*qdwin: mapped handle=/, "", h); sub(/ .*/, "", h)
            if (h in mine) { print "mapped handle=" h " pid=" want; found = 1; exit }
        }
        END { if (!found) { printf "no mapped toplevel for pid %s yet", want; exit 1 } }'
}

# qdwin_stop_admin_app — stop every admin app instance and wait until gone.
# (`-u admin` keeps pkill -f from matching the root vm-exec shell itself.)
qdwin_stop_admin_app() {
    local i
    pkill -u admin -f '[q]distro-admin-approval-app|[q]distro_admin_app' 2>/dev/null || true
    for i in $(seq 1 50); do
        pgrep -u admin -f '[q]distro-admin-approval-app|[q]distro_admin_app' >/dev/null || return 0
        sleep 0.1
    done
    pkill -KILL -u admin -f '[q]distro-admin-approval-app|[q]distro_admin_app' 2>/dev/null || true
    sleep 0.5
    ! pgrep -u admin -f '[q]distro-admin-approval-app|[q]distro_admin_app' >/dev/null
}

# ---- system-bus trace of the broker interface ------------------------------
#
# A root dbus-monitor on org.qdistro.AdminBroker1 (method calls AND signals,
# including the unicast calls apps make to the broker's unique name). It is
# the evidence for "which client called what, when": the admin app and the TUI
# each refresh their queue with GetPending and decide with DecideRequest, and
# the broker announces RequestPending / RequestDecided. A trace is attached
# only once it has seen a private QciProbe signal sent after it started, so an
# empty trace can never mean "not listening".
: "${QCI_BUS_TRACE_DIR:=/run/qci-bus-trace}"
: "${QCI_GUEST_PYTHON:=/usr/bin/python3}"

_qci_bus_trace_tag_ok() {
    case "$1" in
        ''|*[!A-Za-z0-9_-]*) printf 'ERROR: bus trace tag must match [A-Za-z0-9_-]+ (got %s)\n' "${1:-nothing}" >&2; return 2 ;;
    esac
}

# qci_bus_trace_start <tag> — start a trace into $QCI_BUS_TRACE_DIR/<tag>.log
# and return once it is proven attached (bounded, 10 s).
qci_bus_trace_start() {
    local tag=$1 d=$QCI_BUS_TRACE_DIR tok i p
    _qci_bus_trace_tag_ok "$tag" || return 2
    install -d -m 0700 "$d" || return 1
    qci_bus_trace_stop "$tag" >/dev/null 2>&1 || true
    rm -f -- "$d/$tag.log" "$d/$tag.pid" "$d/$tag.start" "$d/$tag.t0"
    # shellcheck disable=SC2016 # $$ and $1.. expand in the inner shell
    setsid -f /bin/sh -c 'echo $$ >"$1"; exec dbus-monitor --system "$2" "$3" "$4" >"$5" 2>&1 </dev/null' \
        _ "$d/$tag.pid" \
        "type='method_call',interface='org.qdistro.AdminBroker1'" \
        "type='signal',interface='org.qdistro.AdminBroker1'" \
        "type='signal',interface='org.qdistro.QciProbe',member='Ready'" \
        "$d/$tag.log"
    tok="qci-trace-$tag-$$-$RANDOM"
    for ((i = 0; i < 100; i++)); do
        dbus-send --system --type=signal /org/qdistro/QciProbe \
            org.qdistro.QciProbe.Ready "string:$tok" 2>/dev/null
        if grep -q -- "$tok" "$d/$tag.log" 2>/dev/null; then
            p=$(cat "$d/$tag.pid" 2>/dev/null)
            _qci_proc_starttime "$p" > "$d/$tag.start" 2>/dev/null || true
            date +%s > "$d/$tag.t0"
            printf 'bus trace %s attached (dbus-monitor pid %s, log %s)\n' "$tag" "$p" "$d/$tag.log"
            return 0
        fi
        sleep 0.1
    done
    printf 'ERROR: bus trace %s never saw its own readiness probe; dbus-monitor output:\n' "$tag" >&2
    head -20 "$d/$tag.log" >&2 2>/dev/null || true
    qci_bus_trace_stop "$tag" >/dev/null 2>&1 || true
    return 1
}

# qci_bus_trace_stop <tag> — stop the trace's dbus-monitor (pid + starttime +
# comm identity); the log is kept.
qci_bus_trace_stop() {
    local tag=$1 d=$QCI_BUS_TRACE_DIR p start i
    _qci_bus_trace_tag_ok "$tag" || return 2
    p=$(cat "$d/$tag.pid" 2>/dev/null) || return 0
    case "$p" in ''|0|1|*[!0-9]*) return 0 ;; esac
    start=$(cat "$d/$tag.start" 2>/dev/null)
    [ "$(cat "/proc/$p/comm" 2>/dev/null)" = dbus-monitor ] || return 0
    [ -z "$start" ] || [ "$(_qci_proc_starttime "$p" 2>/dev/null)" = "$start" ] || return 0
    kill "$p" 2>/dev/null || true
    for ((i = 0; i < 50; i++)); do
        [ -d "/proc/$p" ] || return 0
        sleep 0.1
    done
    kill -KILL "$p" 2>/dev/null || true
    return 0
}

# _qci_proc_starttime <pid> — field 22 of /proc/<pid>/stat (after the comm).
_qci_proc_starttime() {
    local s
    s=$(cat "${QCI_PROC_ROOT:-/proc}/$1/stat" 2>/dev/null) || return 1
    s=${s##*) }
    # shellcheck disable=SC2086 # split the stat fields on purpose
    set -- $s
    printf '%s\n' "${20}"
}

# qci_bus_names_of_pid <pid> — the unique system-bus names a live process owns
# (one per connection), from the bus's own credentials.
qci_bus_names_of_pid() {
    local want=$1
    case "$want" in ''|*[!0-9]*) printf 'ERROR: qci_bus_names_of_pid needs a pid\n' >&2; return 2 ;; esac
    busctl --system list --unique --no-legend --no-pager 2>/dev/null \
        | awk -v p="$want" '$2 == p { print $1 }'
}

# qci_bus_trace_events <tag> — the trace as TSV, one line per message:
#   <epoch.us>\t<call|signal>\t<sender>\t<member>\t<args, comma-joined>
qci_bus_trace_events() {
    local tag=$1
    _qci_bus_trace_tag_ok "$tag" || return 2
    [ -r "$QCI_BUS_TRACE_DIR/$tag.log" ] || {
        printf 'ERROR: no bus trace log for %s (start it with qci_bus_trace_start)\n' "$tag" >&2
        return 1
    }
    "$QCI_GUEST_PYTHON" -I - "$QCI_BUS_TRACE_DIR/$tag.log" <<'PY'
import re, sys
HDR = re.compile(r"^(method call|signal) time=(\d+\.\d+) sender=(\S+) -> "
                 r"destination=.*?member=(\S+)\s*$")
ARG = re.compile(r"^\s+(?:int32|uint32|int64|uint64|string|boolean|double) (.*)$")
cur = None
def flush():
    if cur:
        print("\t".join([cur[0], cur[1], cur[2], cur[3], ",".join(cur[4])]))
for line in open(sys.argv[1], errors="replace"):
    m = HDR.match(line)
    if m:
        flush()
        kind = "call" if m.group(1) == "method call" else "signal"
        cur = [m.group(2), kind, m.group(3), m.group(4), []]
        continue
    a = ARG.match(line)
    if cur and a and len(cur[4]) < 8:
        cur[4].append(a.group(1).strip().strip('"'))
flush()
PY
}

# qci_bus_trace_count <tag> <call|signal> <member> <pid|-> [since-epoch]
# Print how many <member> messages of that kind the trace holds, sent by any
# connection of the live process <pid> ("-": any sender), at or after <since>.
qci_bus_trace_count() {
    local tag=$1 kind=$2 member=$3 pid=$4 since=${5:-0} names
    case "$kind" in call|signal) ;; *) printf 'ERROR: kind must be call or signal\n' >&2; return 2 ;; esac
    if [ "$pid" = - ]; then
        names=-
    else
        names=$(qci_bus_names_of_pid "$pid" | paste -sd ' ') || return 1
        [ -n "$names" ] || { printf 'ERROR: pid %s owns no system-bus connection (is it still running?)\n' "$pid" >&2; return 1; }
    fi
    qci_bus_trace_events "$tag" | awk -F'\t' -v k="$kind" -v m="$member" -v n="$names" -v s="$since" '
        BEGIN { if (n != "-") { c = split(n, a, " "); for (i = 1; i <= c; i++) own[a[i]] = 1 } }
        $2 == k && $4 == m && ($1 + 0) >= (s + 0) && (n == "-" || ($3 in own)) { cnt++ }
        END { print cnt + 0 }'
}

# ---- the admin TUI, hosted in foot -----------------------------------------
#
# The Textual admin TUI ships as /usr/local/bin/qdistro-admin-tui, a symlink
# into the root-owned /usr/local/lib/qdistro/admin-tui/ that
# scripts/install/install-admin-cli-for-vm.sh lays down (the image chain runs
# the same installer). The broker lets an admin-uid peer exercise authority
# only as THAT script run `/usr/bin/python3 -I <path>`, with no arguments and an
# allowlisted environment (_peer_is_installed_admin_script); the entry point
# re-execs itself into that shape (_reexec_for_authority). So the scenarios
# start the installed command with no arguments and verify the resulting
# process -- never a dev copy, a wrapper, or root.
#
# The terminal is `foot`, a test client every golden bakes
# (fresh-vm-bootstrap.sh, "Core test clients") and the image does NOT ship:
# the shipped terminal is qterminator. The scenarios test the TUI, not a
# terminal, and give no qterminator coverage.
#
# One TUI per scenario VM. Its identity lives in a root-only state directory so
# every later guest shell (one per vm-exec) reads the same processes:
#   title  foot.pid foot.start  tui.pid tui.start  tui.bus  foot.log
# and the broker bus trace `admin-tui` started with it records the traffic the
# scenarios use as evidence (qdwin_admin_tui_* below).
: "${QCI_ADMIN_TUI_STATE:=/run/qci-admin-tui}"
: "${QCI_ADMIN_TUI_ENTRY:=/usr/local/bin/qdistro-admin-tui}"
: "${QCI_ADMIN_TUI_LIB:=/usr/local/lib/qdistro/admin-tui/qdistro_admin_tui.py}"
: "${QCI_ADMIN_TUI_SRC:=/root/qdistro-src/tui/qdistro_admin_tui.py}"
: "${QCI_ADMIN_TUI_BROKER_MODULE:=/usr/libexec/qdistro/qdistro_admin_broker.py}"
: "${QCI_ADMIN_TUI_PYTHON:=/usr/bin/python3}"
: "${QCI_ADMIN_TUI_FOOT:=foot}"

# qdwin_admin_tui_check_install — the installed TUI is the installer's, intact,
# and runnable: the entry symlink points at the root-owned module (not a
# wrapper written THROUGH the symlink, which is what the retired golden step
# did), the module matches the source tree when that is present, Textual and
# Rich import under the system interpreter, and foot exists. Every failure is
# a bake regression: report ERROR, never skip.
qdwin_admin_tui_check_install() {
    local entry=$QCI_ADMIN_TUI_ENTRY lib=$QCI_ADMIN_TUI_LIB owner=${QCI_ADMIN_TUI_OWNER_UID:-0}
    local target f st uid mode first
    target=$(readlink -- "$entry" 2>/dev/null) || target=""
    if [ "$target" != "$lib" ]; then
        printf "ERROR: %s is not the installer's symlink to %s (readlink: %s)\n" \
            "$entry" "$lib" "${target:-not a symlink}" >&2
        return 1
    fi
    if [ -L "$lib" ] || [ ! -f "$lib" ]; then
        printf 'ERROR: %s is not a regular file\n' "$lib" >&2
        return 1
    fi
    for f in "$lib" "$(dirname -- "$lib")"; do
        st=$(stat -c '%u %a' -- "$f" 2>/dev/null) || { printf 'ERROR: cannot stat %s\n' "$f" >&2; return 1; }
        uid=${st%% *}; mode=${st##* }
        if [ "$uid" != "$owner" ]; then
            printf 'ERROR: %s is owned by uid %s, not %s\n' "$f" "$uid" "$owner" >&2
            return 1
        fi
        if (( (8#$mode & 8#022) != 0 )); then
            printf 'ERROR: %s is group/other writable (mode %s)\n' "$f" "$mode" >&2
            return 1
        fi
    done
    IFS= read -r first < "$lib" || first=""
    if [ "$first" != '#!/usr/bin/python3 -I' ] || ! grep -q '^def _reexec_for_authority' "$lib"; then
        printf 'ERROR: %s is not the installed TUI module (first line: %s); a wrapper replaced it\n' \
            "$lib" "$first" >&2
        return 1
    fi
    if [ -f "$QCI_ADMIN_TUI_SRC" ]; then
        cmp -s -- "$lib" "$QCI_ADMIN_TUI_SRC" || {
            printf 'ERROR: %s differs from the source tree %s\n' "$lib" "$QCI_ADMIN_TUI_SRC" >&2
            return 1
        }
    fi
    if ! "$QCI_ADMIN_TUI_PYTHON" -I -c 'import textual, rich' >/dev/null 2>&1; then
        printf 'ERROR: Textual/Rich do not import under %s -I: the golden must bake python314-textual and python314-rich (fresh-vm-bootstrap.sh); a bake regression, not a skip\n' \
            "$QCI_ADMIN_TUI_PYTHON" >&2
        return 1
    fi
    command -v "$QCI_ADMIN_TUI_FOOT" >/dev/null 2>&1 || {
        printf 'ERROR: foot is missing: it is a core test client of every golden (fresh-vm-bootstrap.sh)\n' >&2
        return 1
    }
    printf 'admin TUI install OK: %s -> %s\n' "$entry" "$lib"
}

# qdwin_admin_tui_identity <tui-pid> [foot-pid] — the process IS the installed
# TUI in the shape the broker admits for authority: the root-owned system
# python3 started `-I <installed path>` with no arguments, uid 1000, an
# environment inside the broker's own allowlist (read from the installed
# broker module, so this cannot drift from what the broker checks), and,
# given <foot-pid>, a child of that terminal. Prints the evidence; 0 = OK.
qdwin_admin_tui_identity() {
    "$QCI_GUEST_PYTHON" -I - "${QCI_PROC_ROOT:-/proc}" "$1" "${2:-}" \
        "$QCI_ADMIN_TUI_ENTRY" "$QCI_ADMIN_TUI_BROKER_MODULE" "${QCI_ADMIN_TUI_UID:-1000}" <<'PY'
import ast, os, re, sys
proc, tpid, fpid, entry, broker, want_uid = sys.argv[1:7]

def fail(msg):
    print(f"identity: FAIL pid={tpid}: {msg}")
    sys.exit(1)

if not tpid.isdigit():
    fail("not a pid")

def constants(path):
    try:
        tree = ast.parse(open(path).read(), path)
    except (OSError, SyntaxError) as e:
        fail(f"cannot read the broker's allowlist from {path}: {e}")
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("_ADMIN_CONTROL_ENV_ALLOW", "_ADMIN_CONTROL_QPA_VALUES",
                        "_ADMIN_CONTROL_ENV_ALLOW_PREFIX", "_POLKIT_AGENT_EXE_DIRS"):
                v = node.value
                if isinstance(v, ast.Call) and v.args:
                    v = v.args[0]
                out[name] = ast.literal_eval(v)
    missing = {"_ADMIN_CONTROL_ENV_ALLOW", "_ADMIN_CONTROL_QPA_VALUES",
               "_ADMIN_CONTROL_ENV_ALLOW_PREFIX", "_POLKIT_AGENT_EXE_DIRS"} - set(out)
    if missing:
        fail(f"broker module {path} lacks {sorted(missing)}")
    return out

c = constants(broker)
base = os.path.join(proc, tpid)
try:
    exe = os.readlink(os.path.join(base, "exe"))
    argv = [a.decode(errors="replace")
            for a in open(os.path.join(base, "cmdline"), "rb").read().split(b"\0")[:-1]]
    env = {}
    for e in open(os.path.join(base, "environ"), "rb").read().split(b"\0"):
        if b"=" in e:
            k, v = e.split(b"=", 1)
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    status = open(os.path.join(base, "status")).read()
    stat = open(os.path.join(base, "stat")).read()
except OSError as e:
    fail(f"unreadable: {e}")

if not (re.fullmatch(r"python3(\.\d+)?", os.path.basename(exe))
        and os.path.dirname(exe) in c["_POLKIT_AGENT_EXE_DIRS"]
        and " (deleted)" not in exe):
    fail(f"exe {exe!r} is not the root-owned system python3")
if argv[1:] != ["-I", entry]:
    fail(f"argv {argv!r} is not [python3, -I, {entry}] (no TUI arguments, isolated)")
uids = next((l.split()[1:] for l in status.splitlines() if l.startswith("Uid:")), [])
if not uids or set(uids[:2]) != {want_uid}:
    fail(f"real/effective uid {uids[:2]} is not {want_uid}")
bad = sorted(k for k in env if k not in c["_ADMIN_CONTROL_ENV_ALLOW"]
             and not k.startswith(c["_ADMIN_CONTROL_ENV_ALLOW_PREFIX"]))
if bad:
    fail(f"environment outside the broker allowlist: {bad} (not yet re-exec'd?)")
if "QT_QPA_PLATFORM" in env and env["QT_QPA_PLATFORM"] not in c["_ADMIN_CONTROL_QPA_VALUES"]:
    fail(f"QT_QPA_PLATFORM={env['QT_QPA_PLATFORM']!r} not allowed")
term = env.get("TERM", "")
if term in ("", "dumb"):
    fail(f"TERM={term!r}: the terminal environment did not survive the re-exec")
ppid = stat.rsplit(")", 1)[1].split()[1]
if fpid and ppid != fpid:
    fail(f"parent pid {ppid} is not the foot terminal {fpid}")
print(f"identity: OK pid={tpid} exe={exe} argv={argv} uid={want_uid} "
      f"ppid={ppid} TERM={term} COLORTERM={env.get('COLORTERM', '')} "
      f"LANG={env.get('LANG', '')} env_keys={sorted(env)}")
PY
}

# _qdwin_admin_tui_state <name> — one recorded value, or fail naming it.
_qdwin_admin_tui_state() {
    local v
    v=$(cat "$QCI_ADMIN_TUI_STATE/$1" 2>/dev/null) || v=""
    [ -n "$v" ] || { printf 'ERROR: no admin TUI recorded (%s/%s missing); start it with qdwin_start_admin_tui\n' "$QCI_ADMIN_TUI_STATE" "$1" >&2; return 1; }
    printf '%s\n' "$v"
}

_probe_admin_tui_child() {
    local fpid=$1 kids n out
    kids=$(pgrep -P "$fpid" 2>/dev/null | paste -sd ' ')
    n=$(printf '%s' "$kids" | wc -w)
    if [ "$n" -ne 1 ]; then
        printf 'foot pid %s has %s child process(es): [%s]' "$fpid" "$n" "$kids"
        return 1
    fi
    out=$(qdwin_admin_tui_identity "$kids" "$fpid") || { printf '%s' "$out"; return 1; }
    printf '%s\n' "$kids" > "$QCI_ADMIN_TUI_STATE/tui.pid"
    printf '%s' "$out"
}

_probe_admin_tui_mounted() {
    local tpid=$1 names name
    names=$(qci_bus_names_of_pid "$tpid")
    [ -n "$names" ] || { printf 'TUI pid %s has no system-bus connection yet' "$tpid"; return 1; }
    for name in $names; do
        if qci_bus_trace_events admin-tui 2>/dev/null \
                | awk -F'\t' -v n="$name" '$2 == "call" && $3 == n && $4 == "GetPending" { f = 1 } END { exit !f }'; then
            printf '%s\n' "$name" > "$QCI_ADMIN_TUI_STATE/tui.bus"
            printf 'TUI connection %s made its first GetPending (mounted, subscribed)' "$name"
            return 0
        fi
    done
    printf 'TUI connection(s) %s made no GetPending yet' "$names"
    return 1
}

# qdwin_start_admin_tui [tag] — start foot running the INSTALLED TUI (no
# arguments) as the admin uid on wayland-1, and return once it is ready:
#   1. foot's toplevel (unique, locked title) is mapped by the compositor;
#   2. foot's one child IS the installed TUI in the broker's authority shape
#      (qdwin_admin_tui_identity, after its re-exec);
#   3. that TUI has mounted: its first GetPending is in the bus trace, which
#      on_mount sends only after subscribing to RequestPending/RequestDecided.
# Prints title/foot_pid/tui_pid/tui_bus. Fails (bounded, naming what it saw)
# otherwise. foot is a golden-only test client; the TUI is the shipped one.
qdwin_start_admin_tui() {
    local tag=${1:-tui} st=$QCI_ADMIN_TUI_STATE title h fpid tpid argv
    case "$tag" in ''|*[!A-Za-z0-9_-]*) printf 'usage: qdwin_start_admin_tui [tag: A-Za-z0-9_-]\n' >&2; return 2 ;; esac
    qdwin_admin_tui_check_install || return 1
    if pgrep -u 1000 -f '[q]distro-admin-tui|[q]distro_admin_tui' >/dev/null 2>&1; then
        printf 'ERROR: an admin TUI is already running (qdwin_stop_admin_tui first): %s\n' \
            "$(pgrep -a -u 1000 -f '[q]distro-admin-tui|[q]distro_admin_tui' | paste -sd ';')" >&2
        return 1
    fi
    rm -rf -- "$st"
    install -d -m 0700 "$st" || return 1
    title="qci-admin-tui-$tag-$(date +%s)-$$"
    printf '%s\n' "$title" > "$st/title"
    qci_bus_trace_start admin-tui >/dev/null || return 1
    # Root opens the log in its own 0700 directory; foot inherits the fd, so no
    # admin-controlled path is ever opened as root. The environment is a clean
    # session subset: the TUI re-execs into the broker allowlist anyway, and
    # foot sets TERM/COLORTERM for its child. locked-title keeps the window's
    # title ours, so focus and capture target exactly this terminal.
    setsid -f runuser -u admin -- env -i HOME=/home/admin USER=admin LOGNAME=admin \
        SHELL=/bin/bash PATH=/usr/bin:/bin LANG=C.UTF-8 \
        XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
        DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus XDG_SESSION_TYPE=wayland \
        "$QCI_ADMIN_TUI_FOOT" --app-id=qci-admin-tui --title="$title" --maximized \
            --override=locked-title=yes -- "$QCI_ADMIN_TUI_ENTRY" \
        </dev/null >"$st/foot.log" 2>&1
    if ! QCI_AWAIT_QUIET=1 await_qdwin_window_title "$title" 30 0.5 >/dev/null; then
        printf 'ERROR: foot never mapped a window titled %s; foot log:\n' "$title" >&2
        sed 's/^/  foot: /' "$st/foot.log" >&2 2>/dev/null || true
        return 1
    fi
    h=$(qdwin_window_handle "$title") || return 1
    fpid=$(qdwin_windows | awk -F'\t' -v h="$h" '$1 == h { print $2 }')
    argv=$(tr '\0' ' ' < "/proc/$fpid/cmdline" 2>/dev/null)
    if [ "$(cat "/proc/$fpid/comm" 2>/dev/null)" != foot ] \
            || [ "$(stat -c %u "/proc/$fpid" 2>/dev/null)" != 1000 ] \
            || [[ "$argv" != *"--title=$title "* ]]; then
        printf "ERROR: window %s (pid %s) is not this launch's foot (argv: %s)\n" "$h" "$fpid" "$argv" >&2
        return 1
    fi
    QCI_AWAIT_QUIET=1 await_qdwin_window_mapped_pid "$fpid" 30 0.2 >/dev/null || return 1
    printf '%s\n' "$fpid" > "$st/foot.pid"
    _qci_proc_starttime "$fpid" > "$st/foot.start" || return 1
    if ! QCI_AWAIT_QUIET=1 _await "the installed admin TUI as foot pid $fpid's child" 30 0.5 \
            _probe_admin_tui_child "$fpid" >/dev/null; then
        sed 's/^/  foot: /' "$st/foot.log" >&2 2>/dev/null || true
        return 1
    fi
    tpid=$(cat "$st/tui.pid")
    _qci_proc_starttime "$tpid" > "$st/tui.start" || return 1
    if ! QCI_AWAIT_QUIET=1 _await "admin TUI pid $tpid mounted (first GetPending on the bus)" 30 0.5 \
            _probe_admin_tui_mounted "$tpid" >/dev/null; then
        sed 's/^/  foot: /' "$st/foot.log" >&2 2>/dev/null || true
        return 1
    fi
    printf 'title=%s\nfoot_pid=%s\ntui_pid=%s\ntui_bus=%s\n' "$title" "$fpid" "$tpid" "$(cat "$st/tui.bus")"
    qdwin_admin_tui_identity "$tpid" "$fpid"
}

# qdwin_admin_tui_alive — the SAME foot and TUI processes recorded at start are
# still running (pid AND starttime: a relaunch, or a recycled pid, fails), and
# the TUI still passes qdwin_admin_tui_identity. Prints the evidence.
qdwin_admin_tui_alive() {
    local fpid tpid fst tst
    fpid=$(_qdwin_admin_tui_state foot.pid) || return 1
    tpid=$(_qdwin_admin_tui_state tui.pid) || return 1
    fst=$(_qdwin_admin_tui_state foot.start) || return 1
    tst=$(_qdwin_admin_tui_state tui.start) || return 1
    if [ "$(_qci_proc_starttime "$fpid" 2>/dev/null)" != "$fst" ]; then
        printf 'ERROR: the recorded foot (pid %s, start %s) is gone\n' "$fpid" "$fst" >&2
        return 1
    fi
    if [ "$(_qci_proc_starttime "$tpid" 2>/dev/null)" != "$tst" ]; then
        printf 'ERROR: the recorded admin TUI (pid %s, start %s) is gone or was relaunched\n' "$tpid" "$tst" >&2
        return 1
    fi
    printf 'same processes: foot pid %s (start %s), TUI pid %s (start %s)\n' "$fpid" "$fst" "$tpid" "$tst"
    qdwin_admin_tui_identity "$tpid" "$fpid"
}

# qdwin_focus_admin_tui [timeout] — keyboard focus on THIS launch's foot
# window (its locked title AND its pid), proven by the compositor's focus
# line, then the same-process check. Call it right before every key burst.
qdwin_focus_admin_tui() {
    local title fpid
    title=$(_qdwin_admin_tui_state title) || return 1
    fpid=$(_qdwin_admin_tui_state foot.pid) || return 1
    qdwin_focus_window "$title" "${1:-15}" "$fpid" || return 1
    qdwin_admin_tui_alive >/dev/null || return 1
}

# qdwin_stop_admin_tui — stop the recorded foot and TUI (identity-checked),
# make sure no admin TUI of any launch survives, and stop the bus trace. The
# logs ($QCI_ADMIN_TUI_STATE/foot.log, the trace log) are kept for artifacts.
qdwin_stop_admin_tui() {
    local f p start i
    for f in tui foot; do
        p=$(cat "$QCI_ADMIN_TUI_STATE/$f.pid" 2>/dev/null) || continue
        start=$(cat "$QCI_ADMIN_TUI_STATE/$f.start" 2>/dev/null) || continue
        case "$p" in ''|0|1|*[!0-9]*) continue ;; esac
        [ "$(_qci_proc_starttime "$p" 2>/dev/null)" = "$start" ] && kill "$p" 2>/dev/null
    done
    for ((i = 0; i < 50; i++)); do
        pgrep -u 1000 -f '[q]distro-admin-tui|[q]distro_admin_tui|[q]ci-admin-tui' >/dev/null 2>&1 || break
        sleep 0.1
    done
    pkill -KILL -u 1000 -f '[q]distro-admin-tui|[q]distro_admin_tui|[q]ci-admin-tui' 2>/dev/null || true
    qci_bus_trace_stop admin-tui >/dev/null 2>&1 || true
    sleep 0.3
    ! pgrep -u 1000 -f '[q]distro-admin-tui|[q]distro_admin_tui|[q]ci-admin-tui' >/dev/null 2>&1
}

# qdwin_admin_tui_bus_report — the TUI's broker traffic and the broker's
# RequestPending/RequestDecided signals from the `admin-tui` trace, with the
# TUI's 30 s safety-poll ticks, as evidence lines for a transcript.
qdwin_admin_tui_bus_report() {
    local names
    names=$(_qdwin_admin_tui_state tui.bus) || return 1
    qci_bus_trace_events admin-tui | _qdwin_admin_tui_analyse report "$names" 0
}

# qdwin_admin_tui_await_signal_refresh <since-epoch> [timeout]
# Wait until the TUI refreshed its queue BECAUSE of a broker RequestPending
# signal sent at or after <since>: a TUI GetPending within 2 s after that
# signal, more than 3 s from every tick of its 30 s safety poll (which runs
# from its first GetPending). That is the evidence that the signal
# subscription delivered -- a refresh that only coincides with a poll tick
# proves nothing and keeps the wait going; on TIMEOUT the last observation
# says which happened (no signal / no refresh / poll-coincident only).
qdwin_admin_tui_await_signal_refresh() {
    local since=${1:?usage: qdwin_admin_tui_await_signal_refresh <since-epoch> [timeout]} timeout=${2:-30} names
    names=$(_qdwin_admin_tui_state tui.bus) || return 1
    _await "admin TUI refresh driven by a RequestPending signal since $since" "$timeout" 0.5 \
        _probe_admin_tui_signal_refresh "$names" "$since"
}
_probe_admin_tui_signal_refresh() {
    qci_bus_trace_events admin-tui | _qdwin_admin_tui_analyse refresh "$1" "$2"
}

# The TUI's safety-poll grid, shared by qdwin_admin_tui_clear_of_poll and the
# oracle (_qdwin_admin_tui_analyse): a refresh counts as signal-driven only
# when it lands within AFTER of the signal and more than NEAR from a tick of
# the POLL-second grid. clear_of_poll must leave the grid at least NEAR (plus
# a margin) behind, or a refresh right after its return is ruled
# INCONCLUSIVE (permissions-gui/35, full-20261015T112127Z-3016413: it left at
# 2.0 s, the refresh came 0.1 s later, 2.1 s from the tick).
_QDWIN_ADMIN_TUI_POLL_S=30.0
_QDWIN_ADMIN_TUI_NEAR_S=3.0
_QDWIN_ADMIN_TUI_AFTER_S=2.0
_QDWIN_ADMIN_TUI_CLEAR_S=4.0   # NEAR + 1 s margin for the action itself

# qdwin_admin_tui_clear_of_poll [budget-s] — return at a moment that is at
# least _QDWIN_ADMIN_TUI_CLEAR_S (4 s) after a tick of the TUI's 30 s safety poll and leaves <budget-s>
# (default 10, max 26) before the next one (phase taken from its first
# GetPending in the trace), sleeping at most ~28 s. Call it right before the
# action whose SIGNAL-driven effect a step asserts, so no poll can run inside
# the budget (otherwise the signal evidence helpers must call the result
# inconclusive). Prints the phase it left at.
qdwin_admin_tui_clear_of_poll() {
    local budget=${1:-10} names m now
    case "$budget" in ''|*[!0-9]*) budget=10 ;; esac
    [ "$budget" -gt 26 ] && budget=26
    names=$(_qdwin_admin_tui_state tui.bus) || return 1
    m=$(qci_bus_trace_events admin-tui | awk -F'\t' -v n=" $names " '$2 == "call" && $4 == "GetPending" && index(n, " " $3 " ") { print $1; exit }')
    [ -n "$m" ] || { printf 'ERROR: no TUI GetPending in the trace to phase the poll from\n' >&2; return 1; }
    now=$(date +%s.%N)
    "$QCI_GUEST_PYTHON" -I -c '
import sys, time
m, now, budget = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
POLL, CLEAR = float(sys.argv[4]), float(sys.argv[5])
phase = (now - m) % POLL
latest = POLL - budget
wait = 0.0
if phase < CLEAR:
    wait = CLEAR - phase
elif phase > latest:
    wait = POLL + CLEAR - phase
time.sleep(wait)
print("poll phase %.1fs after a tick, %.1fs before the next (waited %.1fs)"
      % ((phase + wait) % POLL, POLL - (phase + wait) % POLL, wait))
' "$m" "$now" "$budget" "$_QDWIN_ADMIN_TUI_POLL_S" "$_QDWIN_ADMIN_TUI_CLEAR_S"
}

# qdwin_admin_tui_await_refresh <since-epoch> [timeout] — readiness only: wait
# until the TUI fetched its queue (GetPending) after a broker RequestPending
# sent at or after <since>, by signal OR safety poll. Use it before capturing a
# state that needs the new row; use qdwin_admin_tui_await_signal_refresh where
# the SIGNAL path itself is the assertion.
qdwin_admin_tui_await_refresh() {
    local since=${1:?usage: qdwin_admin_tui_await_refresh <since-epoch> [timeout]} timeout=${2:-30} names
    names=$(_qdwin_admin_tui_state tui.bus) || return 1
    _await "admin TUI refreshed its queue after a RequestPending since $since" "$timeout" 0.5 \
        _probe_admin_tui_any_refresh "$names" "$since"
}
_probe_admin_tui_any_refresh() {
    qci_bus_trace_events admin-tui | _qdwin_admin_tui_analyse any "$1" "$2"
}

# qdwin_admin_tui_removed_by_signal <since-epoch>
# After a decision made ELSEWHERE (the admin app), prove the TUI's empty queue
# came from the broker's RequestDecided signal and not from a poll: a
# RequestDecided at or after <since> is in the trace, the TUI made NO
# DecideRequest of its own, and every TUI GetPending since <since> (the TUI's
# cheap path removes a known row without one) followed a broker signal within
# 2 s away from a poll tick. Call it AFTER capturing the frame that shows the
# empty queue, so "no poll refresh until now" covers that frame. Exit 0 =
# proven, 1 = FAIL, 3 = INCONCLUSIVE (a safety poll ran in between: ERROR,
# re-run; precede the decision with qdwin_admin_tui_clear_of_poll).
qdwin_admin_tui_removed_by_signal() {
    local since=${1:?usage: qdwin_admin_tui_removed_by_signal <since-epoch>} names
    names=$(_qdwin_admin_tui_state tui.bus) || return 1
    qci_bus_trace_events admin-tui | _qdwin_admin_tui_analyse removed "$names" "$since"
}

# qdwin_admin_tui_decision_evidence <action> <allow|deny> <scope> [caller-uid]
# Prove that THE TUI made the decision the scenario drove with a key, not a
# timeout or another client: the trace holds a DecideRequest(rid, decision,
# scope) from the TUI's own connection (one rid), the broker's RequestDecided
# for that rid follows it, and the permission audit DB records that rid --
# for <action>, since the trace started -- as an admin prompt decision
# (`<0|1>|<scope>|prompt|1000|<caller-uid>`). DENIED alone at the requester
# also means a timeout; this correlation is what ties it to the keypress.
qdwin_admin_tui_decision_evidence() {
    local action=$1 decision=$2 scope=$3 caller=${4:-2000} names rid t0 want row
    case "$decision" in allow) want=1 ;; deny) want=0 ;; *) printf 'usage: qdwin_admin_tui_decision_evidence <action> <allow|deny> <scope> [caller-uid]\n' >&2; return 2 ;; esac
    names=$(_qdwin_admin_tui_state tui.bus) || return 1
    t0=$(cat "$QCI_BUS_TRACE_DIR/admin-tui.t0" 2>/dev/null) || t0=0
    rid=$(qci_bus_trace_events admin-tui | "$QCI_GUEST_PYTHON" -I -c '
import sys
names, decision, scope = set(sys.argv[1].split()), sys.argv[2], sys.argv[3]
ev = [l.rstrip("\n").split("\t") for l in sys.stdin]
ev = [e for e in ev if len(e) == 5]
calls = [e for e in ev if e[1] == "call" and e[3] == "DecideRequest" and e[2] in names]
mine = [e for e in calls if e[4].split(",")[1:3] == [decision, scope]]
rids = sorted({e[4].split(",")[0] for e in mine})
if len(rids) != 1:
    print("FAIL: TUI DecideRequest(%s, %s) calls: %s (want exactly one request id); all TUI DecideRequest: %s"
          % (decision, scope, rids, [e[4] for e in calls]), file=sys.stderr)
    sys.exit(1)
rid = rids[0]
t_call = min(float(e[0]) for e in mine if e[4].split(",")[0] == rid)
sig = [e for e in ev if e[1] == "signal" and e[3] == "RequestDecided"
       and e[4].split(",")[:2] == [rid, decision] and float(e[0]) >= t_call]
if not sig:
    print("FAIL: no RequestDecided(%s, %s) after the TUI call at %.3f" % (rid, decision, t_call), file=sys.stderr)
    sys.exit(1)
print("TUI DecideRequest(%s,%s,%s) at %.3f; broker RequestDecided(%s,%s) at %s"
      % (rid, decision, scope, t_call, rid, decision, sig[0][0]), file=sys.stderr)
print(rid)
' "$names" "$decision" "$scope") || return 1
    case "$rid" in ''|*[!0-9]*) printf 'FAIL: no request id\n' >&2; return 1 ;; esac
    row=$(qci_sqlite audit "SELECT decision, scope, source, approver_uid, caller_uid FROM audit WHERE request_id=$rid AND action='$action' AND ts >= $t0 ORDER BY id DESC LIMIT 1;") || return 1
    printf 'audit(request_id=%s, action=%s): %s\n' "$rid" "$action" "${row:-<none>}"
    if [ "$row" != "$want|$scope|prompt|1000|$caller" ]; then
        printf 'FAIL: expected audit row %s|%s|prompt|1000|%s\n' "$want" "$scope" "$caller" >&2
        return 1
    fi
    printf 'OK: the TUI decided request %s (%s, %s); audited as an admin prompt decision\n' "$rid" "$decision" "$scope"
}

# _qdwin_admin_tui_analyse <report|refresh|removed> <tui-bus-names> <since>
# reads qci_bus_trace_events TSV on stdin. The poll phase is the TUI's first
# GetPending (on_mount refreshes, then starts its 30 s interval).
_qdwin_admin_tui_analyse() {
    "$QCI_GUEST_PYTHON" -I -c '
import sys
mode, names, since = sys.argv[1], set(sys.argv[2].split()), float(sys.argv[3])
POLL, NEAR, AFTER = float(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6])
ev = []
for line in sys.stdin:
    f = line.rstrip("\n").split("\t")
    if len(f) == 5:
        ev.append((float(f[0]), f[1], f[2], f[3], f[4]))
tui = [e for e in ev if e[2] in names]
gets = [e[0] for e in tui if e[1] == "call" and e[3] == "GetPending"]
if not gets:
    print("no GetPending from the TUI connection(s) %s in the trace" % sorted(names)); sys.exit(1)
m = gets[0]
def tick_dist(t):
    k = max(1, round((t - m) / POLL))
    return abs(t - (m + k * POLL))
sigs = [e for e in ev if e[1] == "signal" and e[3] in ("RequestPending", "RequestDecided")]
def driven(g):
    for s in sigs:
        if s[0] <= g <= s[0] + AFTER and tick_dist(g) > NEAR:
            return s
    return None
if mode == "report":
    print("tui_first_getpending=%.3f (poll ticks every %ds after it)" % (m, POLL))
    for e in ev:
        who = "TUI" if e[2] in names else e[2]
        extra = " tick_distance=%.1fs" % tick_dist(e[0]) if e[2] in names and e[3] == "GetPending" and e[0] > m else ""
        print("%.3f %s %s %s(%s)%s" % (e[0], e[1], who, e[3], e[4], extra))
    sys.exit(0)
if mode == "refresh":
    seen = [s for s in sigs if s[3] == "RequestPending" and s[0] >= since]
    if not seen:
        print("no RequestPending signal since %.3f" % since); sys.exit(1)
    notes = []
    for s in seen:
        for g in gets:
            if s[0] <= g <= s[0] + AFTER:
                if tick_dist(g) > NEAR:
                    print("SIGNAL-DRIVEN: RequestPending(%s) at %.3f, TUI GetPending at %.3f (+%.2fs), %.1fs from the nearest safety-poll tick" % (s[4], s[0], g, g - s[0], tick_dist(g)))
                    sys.exit(0)
                notes.append("TUI GetPending at %.3f follows RequestPending(%s) but is %.1fs from a safety-poll tick: INCONCLUSIVE" % (g, s[4], tick_dist(g)))
    print("; ".join(notes) or "RequestPending at %s but no TUI GetPending within %ss after it" % ([round(s[0], 3) for s in seen], AFTER))
    sys.exit(1)
if mode == "any":
    seen = [s for s in sigs if s[3] == "RequestPending" and s[0] >= since]
    if not seen:
        print("no RequestPending signal since %.3f" % since); sys.exit(1)
    after = [g for g in gets if g >= seen[0][0]]
    if not after:
        print("RequestPending(%s) at %.3f; no TUI GetPending after it yet" % (seen[0][4], seen[0][0])); sys.exit(1)
    print("TUI GetPending at %.3f after RequestPending(%s) at %.3f" % (after[0], seen[0][4], seen[0][0]))
    sys.exit(0)
if mode == "removed":
    dec = [s for s in sigs if s[3] == "RequestDecided" and s[0] >= since]
    if not dec:
        print("FAIL: no RequestDecided signal since %.3f" % since); sys.exit(1)
    own = [e for e in tui if e[1] == "call" and e[3] == "DecideRequest"]
    if own:
        print("FAIL: the TUI itself called DecideRequest: %s" % [(round(e[0], 3), e[4]) for e in own]); sys.exit(1)
    late = [g for g in gets if g >= since]
    unexplained = [g for g in late if driven(g) is None]
    polls = [g for g in unexplained if tick_dist(g) <= NEAR]
    if unexplained and len(polls) == len(unexplained):
        print("INCONCLUSIVE: the TUI safety poll ran at %s, after %.3f and before this check; it, not the signal, may have emptied the queue (re-run with less delay between the decision and the frame)" % ([round(g, 3) for g in polls], since)); sys.exit(3)
    if unexplained:
        print("FAIL: TUI GetPending at %s since %.3f explained by neither a broker signal nor the safety poll" % ([round(g, 3) for g in unexplained], since)); sys.exit(1)
    print("OK: RequestDecided(%s) at %.3f; TUI made no DecideRequest; TUI GetPending since then: %s (each within %ss of a broker signal, away from poll ticks)" % (dec[0][4], dec[0][0], [round(g, 3) for g in late] or "none", AFTER))
    sys.exit(0)
print("unknown mode " + mode); sys.exit(2)
' "$@" "$_QDWIN_ADMIN_TUI_POLL_S" "$_QDWIN_ADMIN_TUI_NEAR_S" "$_QDWIN_ADMIN_TUI_AFTER_S"
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
    local tag=$1 user=$2 cmd=$3 base script rc=0
    base=$(_bg_base "$tag") || return 2
    # A driver whose claim was lost must not start more work (see
    # qci_claim_driver); with no claim held this is a no-op.
    _qci_claim_check
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
    # Under a driver claim the job is a worker of the driver and keeps the
    # claim until it exits (qci_claim_driver). It must be on record BEFORE it
    # runs anything, or a driver killed in between would release the claim
    # with the job already filing requests: so the group STOPS itself first,
    # and _qci_claim_register_job continues it once its record is written.
    local gate=''
    [ "${QCI_DRIVER_CLAIM_DEPTH:-0}" -gt 0 ] && gate='kill -STOP "$BASHPID"; '
    printf -v script '{ %s( eval "$QCI_BG_CMD" ) > %q %s < /dev/null; echo $? > %q; mv -f %q %q; } < /dev/null > /dev/null 2>&1 & echo $! > %q' \
        "$gate" "$base.log" "$errspec" "$base.rc.part" "$base.rc.part" "$base.rc" "$base.pid"
    if [ "$user" = "-" ] || [ "$user" = "$(id -un)" ]; then
        QCI_BG_CMD=$cmd bash -c "$script" || rc=$?
    else
        runuser -u "$user" -- env "QCI_BG_CMD=$cmd" bash -c "$script" || rc=$?
    fi
    [ -z "$gate" ] || [ "$rc" -ne 0 ] || _qci_claim_register_job "$tag" "$base.pid"
    return "$rc"
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
# Take a non-blocking exclusive flock for THIS shell and keep it while this
# shell, or a bg_start job it started, is alive. Call it once, directly, at
# the top of the driver — not in a subshell, pipeline, command substitution,
# or `flock -c`. The claim belongs to the process that called it ($BASHPID):
# a short-lived subshell that claims releases the claim when IT exits, and
# the rest of the driver would run with no claim.
#
# Pass a scenario-scoped path: /tmp/qci/<slug>/driver.lock. Under /tmp/qci
# the <slug> directory must already exist (the gate creates it) and any other
# shape is refused with exit 2 (_qci_claim_scenario_path); elsewhere the
# parent directory is created. If the argument is omitted the default is
# /tmp/qci-driver.lock (one lock for the whole guest — prefer the scenario
# path).
#
# The claiming shell, same path: a second call returns 0 and takes nothing
# new. That is what lets one driver claim once at the top and source this
# file again afterwards: sourcing never claims, and it must not clear the
# claim. A different path from that shell is refused without releasing the
# first lock. Any OTHER process — including a subshell or background job of
# the driver, which inherits the claim's shell variables — makes a real
# attempt, so a leftover subshell cannot "re-claim" a lock it never held.
#
# Another live holder: print an ERROR line (plus one line per live process
# the claim is held for) to stderr and `exit 1` the calling shell. Do not
# steal the lock and do not arm a timeout that would kill the first driver.
# `exit` (not `return`) so the lines after the claim do not run even without
# `set -e`, and even under `qci_claim_driver || true` — exit is not caught by
# a conditional. qci_claim_done exits the driver intentionally after its
# foreground work has finished; call it as the final command when an app
# launched by the driver must remain available for inspection.
#
# WHAT HOLDS THE CLAIM, AND FOR HOW LONG. It used to be an fd of the driver
# shell (`exec {fd}>>lock; flock fd`). bash cannot mark that fd
# close-on-exec, so EVERY process the driver started inherited it: an admin
# app from qdistro-start-admin-app (reparented to init), a `setsid -f`
# qdistro-test-window that ignores SIGTERM while it idles in
# wl_display_dispatch. A driver that stopped on a waiter timeout left such an
# app running, the app kept the lock, and every retry was refused "a second
# guest driver is already running" with no driver alive (permissions-gui/04
# and /44, qdwin gui/16, full-20260926T153217Z-3807077).
# Now a HOLDER keeps the lock: two sh processes (_QCI_CLAIM_HOLDER), a
# parent that takes the flock on its own descriptor and a guard child that
# inherits it, both with detached stdio (qga guest-exec waits for every
# holder of the driver's output pipe; the holder is none) and both ignoring
# TERM/INT/HUP. Each keeps the lock while EITHER
#   - the driver shell is alive, by (pid, start time) from /proc — a zombie
#     is dead, as the old fd was released at exit, not at reaping; OR
#   - a bg_start job the driver started is alive. bg_start registers its job
#     (pid, start time, tag) in <lock>.jobs BEFORE the job runs its command
#     (the job waits stopped until then). A job is a worker of the driver
#     and may still file or hold a broker request, so a retry must not run
#     beside it. An app the driver merely LAUNCHED (not through bg_start, or
#     one a bg_start launcher daemonized) is not registered and holds nothing:
#     the retry's Setup stops it.
# The refusal lists the live processes the claim is held for, so a retry
# knows exactly what to stop.
# A root GUI driver enters a dedicated cgroup before qci_claim_driver returns.
# Its holders stay in the parent cgroup. Unexpected owner or guardian death
# triggers cgroup.kill, which also catches children that fork and reparent
# between process scans. A registered bg_start job moves out of that scope
# while stopped, before it runs. Its pending record remains killable by the
# guardians until CONT succeeds and a job record completes the handoff.
# A deliberate qci_claim_done leaves detached
# apps in the scope; they never inherit the flock. A missing or unwritable
# cgroup fails the claim closed. Detached cleaners remove the marker and
# empty scope after both guardians finish; a preserved app delays cleanup
# until it exits. The /proc walker below is used only by the
# unprivileged host bats suite, which cannot create a delegated cgroup.
# FAIL CLOSED: each of the two watches the other. If one dies (SIGKILL)
# while the driver shell lives, the survivor, which still holds the lock,
# kills the driver's cgroup and keeps the lock until it is unpopulated — so a
# contender can never run beside ANY later work of that driver. An app that
# was deliberately left by qci_claim_done is not killed at normal exit. Only
# killing both at the same instant (which also no tree kill does without
# killing the driver) gets past this. The driver also checks both at its
# next bg_start / qci_host_step.
# When the driver shell is dead, the holder removes a `waiting` advertisement
# that driver left (see qci_host_step), so the host never acts on a step of
# a dead driver.
#
# The lock and <lock>.jobs are regular files owned by the claiming user with
# one link. /tmp/qci/<slug>/ is world-writable (sticky): a file another uid
# created there first is theirs to unlink and replace under a live holder,
# so the claim refuses one (exit 2), and creates its own without following a
# symlink. The sticky bit then stops anyone else from swapping it; a
# directory without it must not be group- or other-writable.
#
# The release is up to one poll late. A contender that finds the lock held
# retries for QCI_DRIVER_CLAIM_GRACE seconds (default 2) before it refuses, so
# a driver started right after the previous one exited is not refused by that
# lag. The retry only delays a refusal; it never takes a lock a live holder
# has.
_QCI_CLAIM_POLL=0.2
# sh -c "$_QCI_CLAIM_HOLDER" qci-driver-claim <self> <role> <lock> <flock-mode>
#     <owner-pid> <owner-start> <poll> <scope> <done-marker>
#     [<partner-pid> <partner-start>]
# <self> is this script's own text (the holder starts the guard with it);
# <role> is `holder` (take the lock, start the guard, announce) or `guard`;
# <flock-mode> is `-n` or a -w timeout in seconds. POSIX sh; `read` and the
# expansions are builtins, so the only fork per poll is the `sleep`.
# Exit 75: the lock is held by someone else; 2: cannot open it; 1: the owner
# died before the lock was taken.
# shellcheck disable=SC2016
_QCI_CLAIM_HOLDER='
self=$1 role=$2 lock=$3 mode=$4 owner=$5 ostart=$6 poll=$7
scope=$8 done_marker=$9 partner=${10:-} pstart=${11:-}
other=${12:-} otherstart=${13:-}
reg=$lock.jobs dir=${lock%/*}
trap "" TERM INT HUP
alive() {
    want=$2 st=""
    { read -r st <"/proc/$1/stat"; } 2>/dev/null || return 1
    # Fields after the LAST ") " (comm may hold spaces/parens): $1 is the
    # state, $20 the start time.
    set -f; set -- ${st##*") "}; set +f
    case "$1" in Z|X|x|"") return 1 ;; esac
    [ "${20:-}" = "$want" ]
}
start_of() {
    st=""
    { read -r st <"/proc/$1/stat"; } 2>/dev/null || return 1
    set -f; set -- ${st##*") "}; set +f
    printf "%s" "${20:-}"
}
held_for() {
    alive "$owner" "$ostart" && return 0
    [ -f "$reg" ] || return 1
    while read -r kind pid start tag; do
        case $kind in job|pending) alive "$pid" "$start" && return 0 ;; esac
    done <"$reg"
    return 1
}
ready_job() {
    [ -f "$reg" ] || return 1
    while read -r kind rpid rstart tag; do
        [ "$kind" = job ] && [ "$rpid" = "$1" ] && [ "$rstart" = "$2" ] && return 0
    done <"$reg"
    return 1
}
kill_pending_jobs() {
    [ -f "$reg" ] || return 0
    while read -r kind jpid jstart tag; do
        [ "$kind" = pending ] || continue
        if ready_job "$jpid" "$jstart"; then
            # A committed worker is safe to keep once CONT has made it
            # runnable. If its owner died before CONT, it is still stopped
            # and would otherwise hold the claim forever.
            state=""
            st=""
            { read -r st <"/proc/$jpid/stat"; } 2>/dev/null || continue
            set -f; set -- ${st##*") "}; set +f
            state=$1
            [ "$state" = T ] || [ "$state" = t ] || continue
        fi
        alive "$jpid" "$jstart" && kill -KILL "$jpid" 2>/dev/null || true
    done <"$reg"
}
completed() {
    value=""
    { read -r value <"$done_marker"; } 2>/dev/null || return 1
    [ "$value" = "done $owner $ostart" ]
}
drain_scope() {
    # cgroup.kill is kernel-managed and closes concurrent fork/reparent races.
    # An unavailable or failed write keeps the flock held; releasing it would
    # let a retry overlap work that may still be running.
    { printf "1\n" >"$scope/cgroup.kill"; } 2>/dev/null || return 1
    while :; do
        populated=""
        while read -r key value; do
            case $key in populated) populated=$value ;; esac
        done <"$scope/cgroup.events" 2>/dev/null || return 1
        [ "$populated" = 0 ] && return 0
        sleep "$poll"
    done
}
clean_after_holders() {
    # This child has no flock fd. Both guardians must be gone before the
    # marker can disappear; detached apps may keep the cgroup populated.
    while alive "$partner" "$pstart" || alive "$other" "$otherstart"; do
        sleep "$poll"
    done
    while [ -n "$scope" ] && [ -d "$scope" ]; do
        populated=""
        while read -r key value; do
            case $key in populated) populated=$value ;; esac
        done <"$scope/cgroup.events" 2>/dev/null || { sleep "$poll"; continue; }
        [ "$populated" = 0 ] || { sleep "$poll"; continue; }
        rmdir -- "$scope" 2>/dev/null && break
        sleep "$poll"
    done
    rm -f -- "$done_marker"
}
# Remove a waiting advertisement the (dead) owner left: tokens carry the
# owner pid as <name>.<owner>.<nonce>.
drop_waiting() {
    tok=""
    # The token is written without a newline: read fails at EOF but fills tok.
    { read -r tok <"$dir/waiting" || [ -n "$tok" ]; } 2>/dev/null || return 0
    case $tok in *".$owner."*) rm -f "$dir/waiting" ;; esac
}
is_job() {
    [ -f "$reg" ] || return 1
    while read -r kind jpid jstart tag; do
        case $kind in job|pending)
            [ "$jpid" = "$1" ] && [ "$jstart" = "$2" ] && return 0 ;;
        esac
    done <"$reg"
    return 1
}
tracked_parent_alive() {
    parent=$1
    for member in $tracked; do
        [ "${member%%:*}" = "$parent" ] || continue
        alive "$parent" "${member#*:}" && return 0
    done
    return 1
}
# `tracked` is a space-delimited set of pid:start-time identities. Refresh it
# while the owner is alive, so a process that successfully daemonizes and is
# reparented away drops out. Registered bg_start roots (and therefore their
# subtrees) are deliberately excluded: the jobs file already keeps the claim
# for those and they are allowed to finish after a normal driver exit.
snapshot_tree() {
    tracked=" $owner:$ostart " grew=1
    while [ "$grew" = 1 ]; do
        grew=0
        for d in /proc/[0-9]*; do
            p=${d#/proc/}
            st=""
            { read -r st <"$d/stat"; } 2>/dev/null || continue
            set -f; set -- ${st##*") "}; set +f
            state=$1 ppid=$2 start=${20:-}
            case "$state:$start" in Z:*|X:*|x:*|*:) continue ;; esac
            case " $tracked $$:$selfstart $partner:$pstart " in *" $p:$start "*) continue ;; esac
            tracked_parent_alive "$ppid" || continue
            is_job "$p" "$start" && continue
            tracked="$tracked$p:$start " grew=1
        done
    done
}
# Add children of the last live snapshot without forgetting identities whose
# parent was reparented after the owner died. Once all known identities are
# stopped this reaches a fixed point: no member can fork between the final
# discovery and KILL. A child that has already detached/reparented is outside
# the driver scope by contract.
grow_tree() {
    grew=0
    for d in /proc/[0-9]*; do
        p=${d#/proc/}
        st=""
        { read -r st <"$d/stat"; } 2>/dev/null || continue
        set -f; set -- ${st##*") "}; set +f
        state=$1 ppid=$2 start=${20:-}
        case "$state:$start" in Z:*|X:*|x:*|*:) continue ;; esac
        case " $tracked $$:$selfstart $partner:$pstart " in *" $p:$start "*) continue ;; esac
        tracked_parent_alive "$ppid" || continue
        is_job "$p" "$start" && continue
        tracked="$tracked$p:$start " grew=1
        # Close the interval before the next scan: a newly found forker must
        # not stay runnable long enough to spawn and reap an orphan.
        alive "$p" "$start" && kill -STOP "$p" 2>/dev/null
    done
}
signal_tracked() {
    sig=$1
    for ent in $tracked; do
        p=${ent%%:*}; want=${ent#*:}
        alive "$p" "$want" && kill -"$sig" "$p" 2>/dev/null
    done
}
# Kill and drain the last observed non-daemonized driver scope on either
# guardian loss OR direct driver loss. Identities prevent PID-reuse signals.
# STOP/discover repeats to a fixed point, closing the fork race of a fixed
# two-pass tree walk.
kill_driver_scope() {
    signal_tracked STOP
    while :; do
        grow_tree
        [ "$grew" = 0 ] && break
    done
    signal_tracked KILL
    for ent in $tracked; do
        p=${ent%%:*}; want=${ent#*:}
        while alive "$p" "$want"; do sleep "$poll"; done
    done
}
watch() {
    selfstart=$(start_of $$) || selfstart=""
    tracked=" $owner:$ostart "
    while :; do
        if [ -n "$scope" ]; then
            if [ -n "$partner" ] && ! alive "$partner" "$pstart" && ! completed; then
                drain_scope || { sleep "$poll"; continue; }
                kill_pending_jobs
            elif ! alive "$owner" "$ostart" && ! completed; then
                drain_scope || { sleep "$poll"; continue; }
                kill_pending_jobs
            elif ! alive "$owner" "$ostart"; then
                kill_pending_jobs
            fi
            if ! held_for; then
                completed || drain_scope || { sleep "$poll"; continue; }
                kill_pending_jobs
                break
            fi
            sleep "$poll"
            continue
        fi
        if alive "$owner" "$ostart"; then
            snapshot_tree
            # The owner can die during the /proc walk. Recheck before the
            # held_for decision, or that transition could break the loop and
            # release the flock without draining the snapshot we just pinned.
            if ! alive "$owner" "$ostart"; then
                kill_driver_scope
            elif [ -n "$partner" ] && ! alive "$partner" "$pstart"; then
                kill_driver_scope
            fi
        else
            # The owner may have vanished before this poll, taking ordinary
            # foreground children out of /proc ancestry. The preceding live
            # snapshot pins those identities so they are still drained.
            kill_driver_scope
        fi
        if ! alive "$owner" "$ostart" \
            || { [ -n "$partner" ] && ! alive "$partner" "$pstart"; }; then
            kill_pending_jobs
        fi
        if ! held_for; then
            # Owner death can happen after the last explicit alive check.
            # Drain the pinned scope on this final path before flock closes.
            kill_driver_scope
            break
        fi
        sleep "$poll"
    done
    drop_waiting
}
if [ "$role" = cleaner ]; then
    clean_after_holders
    exit 0
fi
if [ "$role" = guard ]; then
    # Both guardians start a cleaner, so abrupt loss of either during its
    # launch cannot strand an empty scope. Each cleaner waits for BOTH
    # guardians before it may remove the shared marker or cgroup.
    sh -c "$self" qci-claim-cleaner "$self" cleaner "$lock" "$mode" "$owner" "$ostart" "$poll" \
        "$scope" "$done_marker" "$partner" "$pstart" "$$" "$(start_of $$)" \
        9<&- >/dev/null 2>&1 &
    watch
    exit 0
fi
exec 9<"$lock" || exit 2
if [ "$mode" = -n ]; then
    flock -n -E 75 9 || exit $?
else
    flock -w "$mode" -E 75 9 || exit $?
fi
alive "$owner" "$ostart" || exit 1
# Our stdout is the claimant'"'"'s pipe: the guard must not keep it.
sh -c "$self" qci-driver-claim "$self" guard "$lock" "$mode" "$owner" "$ostart" "$poll" \
    "$scope" "$done_marker" "$$" "$(start_of $$)" >/dev/null &
partner=$!
pstart=$(start_of "$partner") || pstart=""
# A detached cleaner does not inherit fd 9 or the claimant output pipe.
sh -c "$self" qci-claim-cleaner "$self" cleaner "$lock" "$mode" "$owner" "$ostart" "$poll" \
    "$scope" "$done_marker" "$$" "$(start_of $$)" "$partner" "$pstart" \
    9<&- >/dev/null 2>&1 &
echo "held $$ $partner" || exit 1
exec >/dev/null
watch
exit 0
'

# _qci_proc_start <pid> — print field 22 of /proc/<pid>/stat (start time);
# nonzero when the process is gone or unreadable.
_qci_proc_start() {
    local st f
    st=""
    { read -r st <"/proc/$1/stat"; } 2>/dev/null || return 1
    # Fields after the LAST ") "; index 19 is field 22. `read -a` splits
    # without globbing.
    f=()
    read -r -a f <<<"${st##*") "}" || true
    case ${f[0]:-} in Z|X|x|'') return 1 ;; esac
    case ${f[19]:-} in ''|*[!0-9]*) return 1 ;; esac
    printf '%s' "${f[19]}"
}

# _qci_claim_safe_file <path> — make sure <path> is a regular file owned by
# this user, with one link, in a directory nobody else can swap it in.
# Creates it (mktemp + link(2), which never follows a symlink) when absent.
_qci_claim_safe_file() {
    local path=$1 dir mode tmp desc
    dir=$(dirname -- "$path")
    mode=$(stat -c '%A %u' -- "$dir" 2>/dev/null) || return 1
    case $mode in
        d????????[tT]\ *) ;;                 # sticky: only the owner unlinks
        d????w????\ *|d???????w?\ *)
            return 1 ;;                      # group/others-writable, not sticky
        d*) ;;
        *) return 1 ;;                       # not a directory (or a symlink)
    esac
    case ${mode#* } in 0|"$EUID") ;; *) return 1 ;; esac
    if [ ! -e "$path" ] && [ ! -L "$path" ]; then
        tmp=$(mktemp -- "$dir/.qci-claim.XXXXXX") || return 1
        ln -T -- "$tmp" "$path" 2>/dev/null || true
        rm -f -- "$tmp"
    fi
    desc=$(stat -c '%F|%u|%h' -- "$path" 2>/dev/null) || return 1
    case $desc in
        "regular file|$EUID|1"|"regular empty file|$EUID|1") return 0 ;;
    esac
    return 1
}

# _qci_claim_take <lock> <owner-pid> <owner-start> <-n | wait-seconds>
#     <scope> <done-marker>
# Start the holder. Returns 0 when it announced the lock
# (QCI_DRIVER_CLAIM_HOLDER is "<holder-pid> <guard-pid>"), 75 when the lock is
# held by someone else, 2 otherwise.
_qci_claim_take() {
    local lock=$1 owner=$2 start=$3 mode=$4 scope=$5 marker=$6 rd line fpid rc=0
    if ! exec {rd}< <(exec sh -c "$_QCI_CLAIM_HOLDER" qci-driver-claim "$_QCI_CLAIM_HOLDER" \
            holder "$lock" "$mode" "$owner" "$start" "$_QCI_CLAIM_POLL" "$scope" "$marker" \
            </dev/null 2>/dev/null); then
        return 2
    fi
    fpid=$!
    IFS= read -r -u "$rd" line || line=""
    exec {rd}<&-
    if [[ "$line" =~ ^held\ ([0-9]+)\ ([0-9]+)$ ]]; then
        QCI_DRIVER_CLAIM_HOLDER="${BASH_REMATCH[1]} ${BASH_REMATCH[2]}"
        return 0
    fi
    wait "$fpid" 2>/dev/null || rc=$?
    [ "$rc" -eq 75 ] && return 75
    return 2
}

# _qci_claim_describe <lock> — one line per live process a refused claim is
# held for, from <lock>.jobs (the previous claimant's record).
_qci_claim_describe() {
    local reg="$1.jobs" kind pid start tag cur n=0
    [ -f "$reg" ] || return 0
    while read -r kind pid start tag; do
        cur=$(_qci_proc_start "$pid") || continue
        [ "$cur" = "$start" ] || continue
        case $kind in
            driver) printf '  held for: the driver shell, pid %s\n' "$pid" ;;
            job) printf '  held for: bg_start job %s, pid %s (stop it, and what it started)\n' \
                    "$tag" "$pid" ;;
            pending) printf '  held for: pending bg_start job %s, pid %s\n' "$tag" "$pid" ;;
            *) continue ;;
        esac
        n=$((n + 1))
    done <"$reg"
    [ "$n" -gt 0 ] || printf '  (no live holder recorded: the previous driver is exiting; retry once)\n'
    return 0
}

# _qci_driver_stop <rc> — stop the driver WITHOUT its EXIT trap: a stopped
# driver leaves the app and requests in place for the host to inspect.
_qci_driver_stop() {
    trap - EXIT
    exit "$1"
}

# _qci_claim_check — when this process tree holds a claim, stop the driver if
# either of the claim's two holder processes has died (the survivor is
# about to SIGKILL this driver anyway; do not start more work meanwhile).
_qci_claim_check() {
    local p s pids starts i
    [ "${QCI_DRIVER_CLAIM_DEPTH:-0}" -gt 0 ] || return 0
    read -r -a pids <<<"${QCI_DRIVER_CLAIM_HOLDER:-}"
    read -r -a starts <<<"${QCI_DRIVER_CLAIM_HOLDER_START:-}"
    for i in 0 1; do
        p=${pids[$i]:-x}
        s=$(_qci_proc_start "$p") || s=""
        if [ -z "$s" ] || [ "$s" != "${starts[$i]:-}" ]; then
            printf 'ERROR: qci_claim_driver: the claim on %s was lost (its holder, pid %s, is gone); stopping this driver\n' \
                "${QCI_DRIVER_CLAIM_PATH:-?}" "$p" >&2
            _qci_driver_stop 1
        fi
    done
    return 0
}

# _qci_proc_state <pid> — print the one-letter state from /proc/<pid>/stat.
_qci_proc_state() {
    local st f
    st=""
    { read -r st <"/proc/$1/stat"; } 2>/dev/null || return 1
    f=()
    read -r -a f <<<"${st##*") "}" || true
    printf '%s' "${f[0]:-}"
}

# _qci_claim_register_job <tag> <pid-file> — the job (bg_start's group) has
# stopped itself before running its command. Record it as a worker of the
# claim, then continue it. If it cannot be recorded, kill it (it never ran)
# and stop the driver without teardown: an unrecorded worker would outlive
# the claim.
_qci_claim_register_job() {
    local tag=$1 pid="" start i state=""
    { read -r pid <"$2"; } 2>/dev/null || pid=""
    case $pid in
        ''|*[!0-9]*)
            printf 'ERROR: bg_start: no pid recorded for job %s; stopping this driver\n' "$tag" >&2
            _qci_driver_stop 1 ;;
    esac
    # Continue only a job that is already stopped: a CONT that overtook its
    # STOP would leave it stopped for ever.
    for i in $(seq 1 100); do
        state=$(_qci_proc_state "$pid") || state=""
        [ "$state" = T ] && break
        sleep 0.05
    done
    if [ "$state" != T ] || ! start=$(_qci_proc_start "$pid") \
        || ! printf 'pending %s %s %s\n' "$pid" "$start" "$tag" >>"$QCI_DRIVER_CLAIM_PATH.jobs"; then
        kill -KILL "$pid" 2>/dev/null
        printf 'ERROR: bg_start: cannot record job %s (pid %s, state %s) in %s.jobs; it was killed before it ran, stopping this driver\n' \
            "$tag" "$pid" "${state:-gone}" "${QCI_DRIVER_CLAIM_PATH:-?}" >&2
        _qci_driver_stop 1
    fi
    if [ -n "${QCI_DRIVER_CLAIM_PARENT_CGROUP:-}" ] \
        && ! { printf '%s\n' "$pid" >"$QCI_DRIVER_CLAIM_PARENT_CGROUP/cgroup.procs"; } 2>/dev/null; then
        kill -KILL "$pid" 2>/dev/null || true
        printf 'ERROR: bg_start: cannot move job %s out of the driver scope; it was killed before it ran\n' "$tag" >&2
        _qci_driver_stop 1
    fi
    # Commit the handoff while the worker is still stopped. Once CONT lets
    # it fork, its whole command must already be registered to hold the claim.
    # A guardian kills a committed but still-stopped worker if the owner dies
    # before CONT, so that this ordering cannot strand the claim.
    if ! printf 'job %s %s %s\n' "$pid" "$start" "$tag" >>"$QCI_DRIVER_CLAIM_PATH.jobs"; then
        kill -KILL "$pid" 2>/dev/null || true
        printf 'ERROR: bg_start: cannot complete job %s handoff; stopping this driver\n' "$tag" >&2
        _qci_driver_stop 1
    fi
    kill -CONT "$pid"
}

_qci_claim_scope_prepare() {
    local owner=$1 start=$2 rel parent scope
    if [ "$EUID" -ne 0 ]; then
        # The host-only bats suite has no delegated cgroup. GUI drivers run
        # as root through qga; a non-root production caller fails closed.
        [ "${QCI_DRIVER_CLAIM_TEST_PROC_FALLBACK:-0}" = 1 ] || return 1
        QCI_DRIVER_CLAIM_SCOPE=
        QCI_DRIVER_CLAIM_PARENT_CGROUP=
        return 0
    fi
    rel=$(awk -F: '$1 == 0 && $2 == "" { print $3 }' /proc/self/cgroup) || return 1
    case $rel in /*) ;; *) return 1 ;; esac
    parent=/sys/fs/cgroup$rel
    scope=$parent/qci-driver-$owner-$start
    mkdir -- "$scope" 2>/dev/null || return 1
    if [ ! -w "$scope/cgroup.kill" ] || [ ! -w "$scope/cgroup.procs" ]; then
        rmdir -- "$scope" 2>/dev/null || true
        return 1
    fi
    QCI_DRIVER_CLAIM_SCOPE=$scope
    QCI_DRIVER_CLAIM_PARENT_CGROUP=$parent
}

qci_claim_done() {
    local rc=${1:-0}
    [ "${QCI_DRIVER_CLAIM_DEPTH:-0}" -gt 0 ] || return 1
    [ "${QCI_DRIVER_CLAIM_OWNER:-}" = "$BASHPID" ] || return 1
    _qci_claim_check
    printf 'done %s %s\n' "$BASHPID" "$QCI_DRIVER_CLAIM_OWNER_START" \
        >"$QCI_DRIVER_CLAIM_DONE_MARKER" || return 1
    _qci_driver_stop "$rc"
}

# _qci_claim_scenario_path <lock> — a lock under the guest scratch root
# (/tmp/qci) must be EXACTLY <root>/<slug>/driver.lock for a <slug> directory
# the gate already created (prepare_guest_scratch). The claim's directory is
# also qci_host_step's: a mistyped slug used to be created silently, the
# driver then advertised `waiting` in that stray directory while the host,
# following the prompt, mkdir'ed every go marker in the real one, and the
# driver stalled for the whole host-step timeout with nothing saying why
# (permissions-gui/25, full-20261001T124446Z-1395361: a retry claimed
# /tmp/qci/qdistro/<slug>/driver.lock). Refuse it up front, naming the
# directories that do exist. Paths outside the root are left alone; a
# non-canonical spelling of a path inside it (/tmp//qci/..., /tmp/./qci/...)
# counts as inside and must then be spelled exactly.
_qci_claim_scenario_path() {
    local lock=$1 root=${QCI_GUEST_SCRATCH_ROOT:-/tmp/qci} rel slug have norm nroot
    case $lock in
        "$root"/*) ;;
        *)
            norm=$(realpath -m -s -- "$lock" 2>/dev/null) || norm=$lock
            nroot=$(realpath -m -s -- "$root" 2>/dev/null) || nroot=$root
            case $norm in
                "$nroot"/*) ;;
                *) return 0 ;;
            esac
            ;;
    esac
    rel=${lock#"$root"/}
    slug=${rel%/driver.lock}
    if [ "$slug/driver.lock" = "$rel" ] && [ -n "$slug" ] \
        && case $slug in */*|.|..) false ;; *) true ;; esac \
        && [ -d "$root/$slug" ] && [ ! -L "$root/$slug" ]; then
        return 0
    fi
    have=$(find "$root" -mindepth 1 -maxdepth 1 -type d -printf '%f ' 2>/dev/null)
    printf 'ERROR: qci_claim_driver: %s is not a scenario lock: use exactly %s/<slug>/driver.lock for a scenario directory the gate created (existing: %s). Fix the path in the driver; do not create the directory.\n' \
        "$lock" "$root" "${have:-none}" >&2
    return 1
}

qci_claim_driver() {
    local lock=${1:-/tmp/qci-driver.lock} dir start marker rc=0 me=$BASHPID

    if [ "${QCI_DRIVER_CLAIM_DEPTH:-0}" -gt 0 ] \
        && [ "${QCI_DRIVER_CLAIM_OWNER:-}" = "$BASHPID" ]; then
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
    _qci_claim_scenario_path "$lock" || exit 2
    if ! mkdir -p -- "$dir" 2>/dev/null; then
        printf 'ERROR: qci_claim_driver: cannot create %s\n' "$dir" >&2
        exit 2
    fi
    if ! _qci_claim_safe_file "$lock" || ! _qci_claim_safe_file "$lock.jobs"; then
        printf 'ERROR: qci_claim_driver: unsafe lock path %s (it and %s.jobs must be regular files owned by uid %s, in a directory others cannot swap them in); not deleting it\n' \
            "$lock" "$lock" "$EUID" >&2
        exit 2
    fi

    # The owner is the process running this function: $BASHPID, not $$ ($$
    # of a subshell is its parent's pid). Read into $me up front: inside a
    # $( ) $BASHPID is the command substitution's own pid.
    if ! start=$(_qci_proc_start "$me"); then
        printf 'ERROR: qci_claim_driver: cannot read the start time of pid %s\n' "$me" >&2
        exit 2
    fi
    if ! _qci_claim_scope_prepare "$me" "$start"; then
        printf 'ERROR: qci_claim_driver: cannot create a killable cgroup for pid %s\n' "$me" >&2
        exit 2
    fi
    marker=$lock.done.$me.$start
    if ! _qci_claim_safe_file "$marker"; then
        [ -z "$QCI_DRIVER_CLAIM_SCOPE" ] || rmdir -- "$QCI_DRIVER_CLAIM_SCOPE" 2>/dev/null || true
        printf 'ERROR: qci_claim_driver: cannot prepare completion marker %s\n' "$marker" >&2
        exit 2
    fi
    if ! : >"$marker"; then
        rm -f -- "$marker" 2>/dev/null || true
        [ -z "$QCI_DRIVER_CLAIM_SCOPE" ] || rmdir -- "$QCI_DRIVER_CLAIM_SCOPE" 2>/dev/null || true
        printf 'ERROR: qci_claim_driver: cannot prepare completion marker %s\n' "$marker" >&2
        exit 2
    fi

    _qci_claim_take "$lock" "$me" "$start" -n "$QCI_DRIVER_CLAIM_SCOPE" "$marker" || rc=$?
    if [ "$rc" -eq 75 ]; then
        # Maybe the previous driver has just exited and its holder has not
        # noticed yet (one poll). Wait that long, never longer.
        rc=0
        _qci_claim_take "$lock" "$me" "$start" "${QCI_DRIVER_CLAIM_GRACE:-2}" "$QCI_DRIVER_CLAIM_SCOPE" "$marker" || rc=$?
    fi
    case $rc in
        0) ;;
        75)
            [ -z "$QCI_DRIVER_CLAIM_SCOPE" ] || rmdir -- "$QCI_DRIVER_CLAIM_SCOPE" 2>/dev/null || true
            rm -f -- "$marker"
            printf 'ERROR: a second guest driver is already running: %s\n' "$lock" >&2
            _qci_claim_describe "$lock" >&2
            exit 1 ;;
        *)
            [ -z "$QCI_DRIVER_CLAIM_SCOPE" ] || rmdir -- "$QCI_DRIVER_CLAIM_SCOPE" 2>/dev/null || true
            rm -f -- "$marker"
            printf 'ERROR: qci_claim_driver: cannot open %s\n' "$lock" >&2
            exit 2 ;;
    esac
    local hp gp hs gs
    read -r hp gp <<<"$QCI_DRIVER_CLAIM_HOLDER"
    if ! hs=$(_qci_proc_start "$hp") || ! gs=$(_qci_proc_start "$gp"); then
        printf 'ERROR: qci_claim_driver: the holder of %s died at once\n' "$lock" >&2
        exit 2
    fi
    QCI_DRIVER_CLAIM_HOLDER_START="$hs $gs"
    if [ -n "$QCI_DRIVER_CLAIM_SCOPE" ] \
        && ! { printf '%s\n' "$me" >"$QCI_DRIVER_CLAIM_SCOPE/cgroup.procs"; } 2>/dev/null; then
        printf 'ERROR: qci_claim_driver: cannot enter driver cgroup %s\n' "$QCI_DRIVER_CLAIM_SCOPE" >&2
        exit 2
    fi
    # A `waiting` advertisement left by a dead driver must not be read as
    # this driver's step (qci_host_step).
    rm -f -- "$dir/waiting"
    # We hold the lock, so every earlier record is dead; start a fresh one.
    if ! printf 'driver %s %s -\n' "$me" "$start" >"$lock.jobs"; then
        printf 'ERROR: qci_claim_driver: cannot write %s.jobs\n' "$lock" >&2
        exit 2
    fi
    QCI_DRIVER_CLAIM_PATH=$lock
    QCI_DRIVER_CLAIM_OWNER=$me
    QCI_DRIVER_CLAIM_OWNER_START=$start
    QCI_DRIVER_CLAIM_DONE_MARKER=$marker
    QCI_DRIVER_CLAIM_DEPTH=1
    return 0
}

# --- Host steps (guest <-> host handshake) -----------------------------------
#
# A host-side step (a screenshot, click, send-key) that must happen in the
# MIDDLE of a guest driver is synchronised through files: the driver says
# which step it waits for; the host acts, then says go. Hand-rolled versions
# failed in full-20260926T153217Z-3807077 in two ways:
#   - qdwin gui/16: the host-marker wait was 30s. The agent ran the driver in
#     the foreground, waited for it to return, and could only act after the
#     driver had already timed out. An agent needs minutes per host step
#     (capture, open the image, decide, click) — seconds are never enough.
#   - permissions-gui/14: the agent did S2-S4 on the host but never sent the
#     S1 go-marker, so the driver sat on S1 until it timed out. Nothing told
#     the host which marker the driver was waiting for.
#
# qci_host_step <name> [timeout]
# Pick a fresh token <name>.<driver pid>.<random>, publish it as the whole
# content of <dir>/waiting, and wait for the host to create the DIRECTORY
# <dir>/<token>.go (`mkdir`, never `touch`: mkdir does not follow a symlink
# someone planted at that path, and the driver only accepts a real directory
# owned by its own uid, so nobody else can forge it). The random token means
# a late go meant for an earlier attempt's step can never release this one.
# <dir> is the directory of the driver's claim (/tmp/qci/<slug>/) unless
# QCI_HOST_STEP_DIR is set. The default timeout is QCI_HOST_STEP_TIMEOUT
# (900s). Returns 0 when go arrived, and <dir>/waiting is removed.
# On TIMEOUT it does not return: it publishes <dir>/<token>.timeout, prints
# an ERROR line, clears the EXIT trap and exits 1 — the driver STOPS and
# leaves the app and requests in place, never falling into its teardown. It
# stops the same way if the driver's claim was lost. So call it directly in
# the driver shell, not in a subshell.
#
# Host side, for each step: poll `cat /tmp/qci/<slug>/waiting` through
# vm-exec until it names the step you are about to act on (the token starts
# with the step name), act, then `mkdir /tmp/qci/<slug>/<token>.go` through
# vm-exec, with the exact token you read.
#
# Files are published with mktemp + rename, never written by path: the
# directory is world-writable (1777), and a root `>` or `touch` follows a
# symlink another uid planted there.
_qci_step_publish() {
    local path=$1 content=$2 tmp
    tmp=$(mktemp "${path%/*}/.qci-step.XXXXXX") || return 1
    if ! printf '%s' "$content" >"$tmp" || ! mv -f -T -- "$tmp" "$path"; then
        rm -f -- "$tmp"
        return 1
    fi
}

# _qci_step_go_ok <path> — the host's go: a real directory (not a symlink)
# owned by this uid.
_qci_step_go_ok() {
    [ -d "$1" ] && [ ! -L "$1" ] && [ "$(stat -c %u -- "$1" 2>/dev/null)" = "$EUID" ]
}

qci_host_step() {
    local name=$1 timeout=${2:-${QCI_HOST_STEP_TIMEOUT:-900}} dir nonce token t0
    case $name in
        ''|*[!A-Za-z0-9_-]*)
            printf 'ERROR: qci_host_step: invalid step name %q: use only [A-Za-z0-9_-]\n' \
                "$name" >&2
            return 2 ;;
    esac
    case $timeout in
        ''|*[!0-9]*)
            printf 'ERROR: qci_host_step: timeout must be whole seconds, got %q\n' "$timeout" >&2
            return 2 ;;
    esac
    dir=${QCI_HOST_STEP_DIR:-}
    if [ -z "$dir" ] && [ -n "${QCI_DRIVER_CLAIM_PATH:-}" ]; then
        dir=$(dirname -- "$QCI_DRIVER_CLAIM_PATH")
    fi
    if [ -z "$dir" ] || [ ! -d "$dir" ]; then
        printf 'ERROR: qci_host_step: no step directory (claim the driver first, or set QCI_HOST_STEP_DIR)\n' >&2
        return 2
    fi
    _qci_claim_check
    nonce=${SRANDOM:-}
    [ -n "$nonce" ] || nonce=$RANDOM$RANDOM
    # <name>.<driver pid>.<nonce>: the claim's holder drops a `waiting` that
    # names a dead driver (qci_claim_driver).
    printf -v token '%s.%s.%s%s' "$name" "${QCI_DRIVER_CLAIM_OWNER:-$$}" "$nonce" "$RANDOM"
    if ! _qci_step_publish "$dir/waiting" "$token"; then
        printf 'ERROR: qci_host_step: cannot publish %s/waiting\n' "$dir" >&2
        return 2
    fi
    printf '[host-step] %s: waiting up to %ss for the host: mkdir %s/%s.go\n' \
        "$name" "$timeout" "$dir" "$token"
    t0=$SECONDS
    while ! _qci_step_go_ok "$dir/$token.go"; do
        _qci_claim_check
        if [ $((SECONDS - t0)) -ge "$timeout" ]; then
            rm -f -- "$dir/waiting"
            _qci_step_publish "$dir/$token.timeout" "" || true
            printf 'ERROR: qci_host_step: the host never created %s/%s.go within %ss; stopping this driver without teardown\n' \
                "$dir" "$token" "$timeout" >&2
            # A host-step timeout is an intentional stop for inspection, not
            # an unexpectedly lost driver. Detached GUI apps may stay up.
            qci_claim_done 1 || true
            _qci_driver_stop 1
        fi
        sleep 1
    done
    rm -f -- "$dir/waiting"
    printf '[host-step] %s: go\n' "$name"
    return 0
}
