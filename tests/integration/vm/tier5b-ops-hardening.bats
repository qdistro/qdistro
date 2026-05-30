#!/usr/bin/env bats
# Tier-5b launcher OPERATIONAL HARDENING lock-in. Like
# bootstrap-hardening.bats this needs NO live VM and no root: it
# exercises the two new shared primitives in lib/spawn-common.sh
# directly (qd_free_space_check, qd_emit_event) with stubs that drive
# both sides of the free-space budget and capture the exact structured
# field set, plus static-invariant checks that spawn-tier5b.sh actually
# WIRES them on the launch / close / error paths.
#
# Why source the helpers instead of running spawn-tier5b.sh --vm: the
# full launcher needs runuser + libvirt + a baked guest image + root,
# none available off a real qdistro host. The load-bearing NEW logic is
# the precheck + the event emitter; those are unit-driveable. The
# wiring (which code path calls them, with which error codes) is pinned
# by mutation-sensitive static assertions below. A real-VM run that
# confirms the fields land in journald and that ENOSPC fails closed is
# tracked as residue.
#
# Run: bats tests/integration/vm/tier5b-ops-hardening.bats

setup() {
    REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" \
                    rev-parse --show-toplevel 2>/dev/null)"
    COMMON="$REPO_ROOT/lib/spawn-common.sh"
    SPAWN="$REPO_ROOT/tier5b-vm/spawn-tier5b.sh"
    [ -f "$COMMON" ] || { echo "spawn-common not found at $COMMON" >&2; return 1; }
    [ -f "$SPAWN" ]  || { echo "spawn-tier5b not found at $SPAWN" >&2; return 1; }
    SINK="$BATS_TEST_TMPDIR/events.log"
    : >"$SINK"
    # Sink stub: a tiny script that appends its single argv (the event
    # MESSAGE) to $SINK so we can assert the EXACT emitted field set.
    SINK_BIN="$BATS_TEST_TMPDIR/sink.sh"
    cat >"$SINK_BIN" <<EOF
#!/bin/bash
printf '%s\n' "\$1" >>"$SINK"
EOF
    chmod +x "$SINK_BIN"
}

# --- 0. syntax ----------------------------------------------------------
@test "tier5b-ops: touched files are syntactically valid" {
    run bash -n "$COMMON"; [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    run bash -n "$SPAWN";  [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    run python3 -c "import ast,sys; ast.parse(open('$REPO_ROOT/tier5b-vm/qdistro_integration.py').read())"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}

# --- A. free-space budget precheck (fail-closed) ------------------------

@test "free-space: passes when avail >= base vsize + headroom" {
    run bash -c '
        . "'"$COMMON"'"
        QDISTRO_BASE_VSIZE_CMD="printf 1000000000" \
        QDISTRO_FREE_BYTES_CMD="printf 4000000000" \
            qd_free_space_check /base /dir 2147483648
    '
    [ "$status" -eq 0 ] || { echo "expected pass (4e9 free > 1e9+2GiB):"$'\n'"$output" >&2; return 1; }
}

@test "free-space: passes at the EXACT budget boundary" {
    # budget = 1000 + 2000 = 3000; avail = 3000 must pass (>=).
    run bash -c '
        . "'"$COMMON"'"
        QDISTRO_BASE_VSIZE_CMD="printf 1000" \
        QDISTRO_FREE_BYTES_CMD="printf 3000" \
            qd_free_space_check /base /dir 2000
    '
    [ "$status" -eq 0 ] || { echo "boundary avail==budget must pass:"$'\n'"$output" >&2; return 1; }
}

@test "free-space: FAILS CLOSED one byte below budget (rc=1)" {
    run bash -c '
        . "'"$COMMON"'"
        QDISTRO_BASE_VSIZE_CMD="printf 1000" \
        QDISTRO_FREE_BYTES_CMD="printf 2999" \
            qd_free_space_check /base /dir 2000
    '
    [ "$status" -eq 1 ] || { echo "expected rc=1 below budget, got $status:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *"free-space precheck"* ]]
    [[ "$output" == *"need 3000B"* ]]
}

@test "free-space: FAILS CLOSED on unparseable free-bytes query" {
    run bash -c '
        . "'"$COMMON"'"
        QDISTRO_BASE_VSIZE_CMD="printf 1000000000" \
        QDISTRO_FREE_BYTES_CMD="printf garbage" \
            qd_free_space_check /base /dir 0
    '
    [ "$status" -eq 1 ] || { echo "garbage avail must fail closed:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *"could not determine"* ]]
}

@test "free-space: FAILS CLOSED on zero/empty base virtual-size query" {
    run bash -c '
        . "'"$COMMON"'"
        QDISTRO_BASE_VSIZE_CMD="printf 0" \
        QDISTRO_FREE_BYTES_CMD="printf 9000000000" \
            qd_free_space_check /base /dir 2147483648
    '
    [ "$status" -eq 1 ] || { echo "zero vsize must fail closed:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *"could not determine"* ]]
}

@test "free-space: headroom is actually added to the budget" {
    # Same vsize + avail, only headroom differs: small headroom passes,
    # large headroom fails — proves headroom is load-bearing, not ignored.
    run bash -c '
        . "'"$COMMON"'"
        QDISTRO_BASE_VSIZE_CMD="printf 1000" QDISTRO_FREE_BYTES_CMD="printf 2000" \
            qd_free_space_check /base /dir 500
    '
    [ "$status" -eq 0 ] || { echo "vsize1000+headroom500 < avail2000 must pass:"$'\n'"$output" >&2; return 1; }
    run bash -c '
        . "'"$COMMON"'"
        QDISTRO_BASE_VSIZE_CMD="printf 1000" QDISTRO_FREE_BYTES_CMD="printf 2000" \
            qd_free_space_check /base /dir 5000
    '
    [ "$status" -eq 1 ] || { echo "vsize1000+headroom5000 > avail2000 must fail:"$'\n'"$output" >&2; return 1; }
}

# --- B. structured event emitter (exact field set) ----------------------

@test "events: launch event emits the exact documented field set" {
    QDISTRO_EVENT_SINK="$SINK_BIN" run bash -c '
        . "'"$COMMON"'"
        qd_emit_event qdistro-tier5b launch \
            TIER=5b VM_NAME=vm-42 APP_ID=firefox \
            ADMIN_UID=1000 MODE=vm LAUNCH_TOKEN=deadbeef
    '
    [ "$status" -eq 0 ]
    run cat "$SINK"
    [ "$output" = "QDISTRO_EVENT=launch TIER=5b VM_NAME=vm-42 APP_ID=firefox ADMIN_UID=1000 MODE=vm LAUNCH_TOKEN=deadbeef" ] \
        || { echo "field set mismatch:"$'\n'"$output" >&2; return 1; }
}

@test "events: close event carries EXIT_REASON and RC" {
    QDISTRO_EVENT_SINK="$SINK_BIN" run bash -c '
        . "'"$COMMON"'"
        qd_emit_event qdistro-tier5b close \
            TIER=5b VM_NAME=vm-42 APP_ID=firefox \
            ADMIN_UID=1000 EXIT_REASON=app-exit RC=0
    '
    [ "$status" -eq 0 ]
    run cat "$SINK"
    [ "$output" = "QDISTRO_EVENT=close TIER=5b VM_NAME=vm-42 APP_ID=firefox ADMIN_UID=1000 EXIT_REASON=app-exit RC=0" ] \
        || { echo "close field set mismatch:"$'\n'"$output" >&2; return 1; }
}

@test "events: error event carries a stable ERROR_CODE and EXIT" {
    QDISTRO_EVENT_SINK="$SINK_BIN" run bash -c '
        . "'"$COMMON"'"
        qd_emit_event qdistro-tier5b error \
            TIER=5b VM_NAME=vm-42 APP_ID=firefox \
            ADMIN_UID=1000 ERROR_CODE=enospc EXIT=4
    '
    [ "$status" -eq 0 ]
    run cat "$SINK"
    [ "$output" = "QDISTRO_EVENT=error TIER=5b VM_NAME=vm-42 APP_ID=firefox ADMIN_UID=1000 ERROR_CODE=enospc EXIT=4" ] \
        || { echo "error field set mismatch:"$'\n'"$output" >&2; return 1; }
}

@test "events: QDISTRO_EVENT is always the first token" {
    QDISTRO_EVENT_SINK="$SINK_BIN" run bash -c '
        . "'"$COMMON"'"
        qd_emit_event qdistro-tier5b launch FOO=bar
    '
    run cat "$SINK"
    [[ "$output" == "QDISTRO_EVENT=launch "* ]] \
        || { echo "QDISTRO_EVENT not first:"$'\n'"$output" >&2; return 1; }
}

@test "events: emit never fails the caller even when the sink errors" {
    # A broken sink (nonexistent command) must still return 0 — an
    # event-emit failure must never abort a launch.
    run bash -c '
        . "'"$COMMON"'"
        QDISTRO_EVENT_SINK=/nonexistent/sink/cmd \
            qd_emit_event qdistro-tier5b launch TIER=5b
        echo "rc=$?"
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"rc=0"* ]]
}

# --- C. spawn-tier5b.sh wiring (mutation-sensitive static invariants) ---

@test "wiring: launch event is emitted right after the launch token" {
    # The launch event must fire once the spawn commits (token + ids
    # resolved), not be silently dropped. Pin that emit_launch_event is
    # both defined and CALLED at top level.
    grep -q "^emit_launch_event\$" "$SPAWN" \
        || { echo "emit_launch_event is never called at top level" >&2; return 1; }
    grep -q "emit_launch_event()" "$SPAWN"
}

@test "wiring: cleanup emits exactly one close event" {
    # The close event lives inside cleanup(), which is re-entrancy
    # guarded (CLEANUP_DONE), so it fires exactly once across EXIT/INT/TERM.
    run awk '/^cleanup\(\) \{/{c=1} c&&/emit_close_event/{print NR; n++} /^}/{if(c){c=0}} END{exit (n==1)?0:1}' "$SPAWN"
    [ "$status" -eq 0 ] || { echo "expected exactly one emit_close_event inside cleanup(), found:"$'\n'"$output" >&2; return 1; }
}

@test "wiring: free-space precheck runs BEFORE qemu-img create" {
    # The precheck call must precede the overlay creation in the --vm
    # path, else we still launch-into-ENOSPC.
    pre=$(grep -n "qd_free_space_check" "$SPAWN" | head -1 | cut -d: -f1)
    cre=$(grep -n "qemu-img create -f qcow2 -F qcow2 -b" "$SPAWN" | head -1 | cut -d: -f1)
    [ -n "$pre" ] || { echo "qd_free_space_check not wired into spawn" >&2; return 1; }
    [ -n "$cre" ] || { echo "qemu-img create line not found" >&2; return 1; }
    [ "$pre" -lt "$cre" ] || { echo "precheck (line $pre) does not precede qemu-img create (line $cre)" >&2; return 1; }
}

@test "wiring: insufficient space fails closed with the enospc error code + exit 4" {
    grep -q 'die_event enospc 4' "$SPAWN" \
        || { echo "enospc fail-closed (die_event enospc 4) missing" >&2; return 1; }
}

@test "wiring: every documented stable ERROR_CODE is actually emitted" {
    for code in enospc overlay-create define start qga-timeout guest-exec client-died; do
        grep -q "die_event $code " "$SPAWN" \
            || { echo "ERROR_CODE '$code' is documented but never emitted" >&2; return 1; }
    done
}

@test "wiring: app-exit close reason is recorded on both normal exit paths" {
    # loopback and --vm normal exits must tag the close event app-exit
    # with the real rc (not the default cleanup/0).
    n=$(grep -c 'CLOSE_REASON=app-exit; CLOSE_RC=\$EXIT' "$SPAWN")
    [ "$n" -eq 2 ] || { echo "expected 2 app-exit close taggings, found $n" >&2; return 1; }
}

@test "wiring: signal teardown tags the close event with the signal reason" {
    grep -q "CLOSE_REASON=signal-int; CLOSE_RC=130" "$SPAWN"
    grep -q "CLOSE_REASON=signal-term; CLOSE_RC=143" "$SPAWN"
}

@test "wiring: header documents the journald event field contract" {
    grep -q "QDISTRO_EVENT=launch" "$SPAWN"
    grep -q "QDISTRO_EVENT=close" "$SPAWN"
    grep -q "QDISTRO_EVENT=error" "$SPAWN"
    grep -q "SYSLOG_IDENTIFIER=qdistro-tier5b" "$SPAWN"
}

@test "decision: App1 ephemeral-registration intent is documented in code" {
    grep -q "EPHEMERAL" "$REPO_ROOT/tier5b-vm/qdistro_integration.py"
    grep -q "re-register" "$REPO_ROOT/tier5b-vm/qdistro_integration.py"
}
