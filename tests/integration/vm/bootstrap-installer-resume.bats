#!/usr/bin/env bats
# Installer-chain step-level rerun / resume tests for
# scripts/install/qdistro-bootstrap.sh (open-followups.md "Bootstrap /
# packaging": step-level rerun controls / installer-chain skip-resume mode).
#
# No live VM and no root. The bootstrap is SOURCED (it guards main() behind
# BASH_SOURCE==$0) so we can drive install_python_modules() directly. The real
# install-*.sh scripts are replaced by a stub tree of executable scripts that
# record which step ran (their basename + the src-dir arg) into a trace file,
# WITHOUT installing anything. We then assert on the EXACT ordered set of steps
# that ran. log/warn/die are tamed; the state dir is redirected into the test
# tmp so chain_state_record writes a real (atomic) file we can inspect.
#
# Run: bats tests/integration/vm/bootstrap-installer-resume.bats

setup() {
    REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" \
                    rev-parse --show-toplevel 2>/dev/null)"
    BOOT="$REPO_ROOT/scripts/install/qdistro-bootstrap.sh"

    # Fake qdistro source tree: $FAKE_QD acts as $REPO_ROOT/qdistro. Install a
    # stub for every chain installer that appends "<name-or-basename> <srcdir>"
    # to $TRACE and exits 0 (success → recorded by chain_state_record).
    FAKE_ROOT="$BATS_TEST_TMPDIR/src"
    FAKE_QD="$FAKE_ROOT/qdistro"
    TRACE="$BATS_TEST_TMPDIR/trace"
    STATE_DIR="$BATS_TEST_TMPDIR/state"
    : > "$TRACE"
    mkdir -p "$FAKE_QD/scripts/install"
    export REPO_ROOT BOOT FAKE_ROOT FAKE_QD TRACE STATE_DIR

    # One executable stub per installer path used by installer_chain_entries.
    # It records the script's basename (so the trace is by-script) plus the
    # src-dir argument it was handed.
    local installers=(
        install-broker-for-qdwin.sh
        install-session-manager.sh
        install-polkit-agent-for-vm.sh
        install-pwd-for-vm.sh
        install-qsu-for-vm.sh
        install-browser-bridge-for-vm.sh
        install-portal-backend-for-vm.sh
        install-phone-for-vm.sh
        install-print-proxy-for-vm.sh
        install-snapshots-for-vm.sh
        install-tier3-for-vm.sh
        install-tier4-host-for-vm.sh
        install-tier5-for-vm.sh
        install-tier5b-for-vm.sh
    )
    local i
    for i in "${installers[@]}"; do
        cat > "$FAKE_QD/scripts/install/$i" <<EOF
#!/bin/bash
echo "$i \$1" >> "$TRACE"
exit 0
EOF
        chmod +x "$FAKE_QD/scripts/install/$i"
    done
}

# Drive install_python_modules() with the chain pointed at the stub tree.
# Args: extra shell to set the rerun-mode globals (e.g. 'RESUME=1').
# Echoes nothing; populates $TRACE + the state file. Returns the rc.
_run_chain() {
    local mode_setup="$1"
    run bash -c '
        source "'"$BOOT"'"
        set +e +u +o pipefail
        log() { :; }; warn() { echo "WARN: $*" >&2; }
        REPO_ROOT="'"$FAKE_ROOT"'"
        QDISTRO_STATE_DIR="'"$STATE_DIR"'"
        CHAIN_STATE_FILE="$QDISTRO_STATE_DIR/installer-chain.state"
        STRICT=""
        '"$mode_setup"'
        install_python_modules
    '
}

# Extract the by-script trace (basenames only, in order).
_trace_scripts() { awk '{print $1}' "$TRACE"; }

# --- baseline: full chain runs in order, state file records every step ----

@test "full chain: all 14 v1 steps run in order and each is recorded in state" {
    _run_chain ''
    [ "$status" -eq 0 ]
    # Exact ordered set of scripts that ran.
    run _trace_scripts
    [ "$status" -eq 0 ]
    expected="install-broker-for-qdwin.sh
install-session-manager.sh
install-polkit-agent-for-vm.sh
install-pwd-for-vm.sh
install-qsu-for-vm.sh
install-browser-bridge-for-vm.sh
install-portal-backend-for-vm.sh
install-phone-for-vm.sh
install-print-proxy-for-vm.sh
install-snapshots-for-vm.sh
install-tier3-for-vm.sh
install-tier4-host-for-vm.sh
install-tier5-for-vm.sh
install-tier5b-for-vm.sh"
    [ "$output" = "$expected" ]

    # State file records every step NAME (not script basename), in order.
    run cat "$STATE_DIR/installer-chain.state"
    [ "$status" -eq 0 ]
    state_expected="broker
session-manager
polkit
pwd
qsu
browser-bridge
portal-backend
phone
print
snapshots
tier3
tier4-host
tier5
tier5b"
    [ "$output" = "$state_expected" ]
}

@test "state record is atomic-ish: only the rename'd file remains, no temp residue" {
    _run_chain ''
    [ "$status" -eq 0 ]
    # No leftover .installer-chain.state.XXXXXX temp files.
    run bash -c 'ls -1 "'"$STATE_DIR"'" | grep -c "^\.installer-chain"'
    [ "$output" = "0" ]
}

# --- per-step src-dir argument is correct (portal/tiers use QD root) -

@test "step src-dir args: subdir steps get \$QD/<subdir>, root steps get \$QD" {
    _run_chain ''
    [ "$status" -eq 0 ]
    grep -qx "install-broker-for-qdwin.sh $FAKE_QD/broker" "$TRACE"
    grep -qx "install-session-manager.sh $FAKE_QD/session_manager" "$TRACE"
    # portal-backend / tier3 / tier4-host / tier5 / tier5b -> bare QD
    grep -qx "install-portal-backend-for-vm.sh $FAKE_QD" "$TRACE"
    grep -qx "install-tier3-for-vm.sh $FAKE_QD" "$TRACE"
}

# --- --resume: skips the recorded-complete prefix, runs only the remainder --

@test "resume: with broker..qsu recorded, runs ONLY browser-bridge..tier5b" {
    mkdir -p "$STATE_DIR"
    printf 'broker\nsession-manager\npolkit\npwd\nqsu\n' \
        > "$STATE_DIR/installer-chain.state"
    _run_chain 'RESUME=1'
    [ "$status" -eq 0 ]
    run _trace_scripts
    expected="install-browser-bridge-for-vm.sh
install-portal-backend-for-vm.sh
install-phone-for-vm.sh
install-print-proxy-for-vm.sh
install-snapshots-for-vm.sh
install-tier3-for-vm.sh
install-tier4-host-for-vm.sh
install-tier5-for-vm.sh
install-tier5b-for-vm.sh"
    [ "$output" = "$expected" ]
}

@test "resume: per-step skip (a recorded mid-chain step is skipped; ALL others run)" {
    # Only 'qsu' recorded. --resume is per-step: it re-runs EVERY step that is
    # not recorded complete (so a mid-chain failure re-runs exactly the gaps),
    # NOT a contiguous prefix. So broker..pwd and browser-bridge..tier5b all
    # run, and ONLY qsu is skipped.
    mkdir -p "$STATE_DIR"
    printf 'qsu\n' > "$STATE_DIR/installer-chain.state"
    _run_chain 'RESUME=1'
    [ "$status" -eq 0 ]
    # qsu skipped ...
    ! grep -q "install-qsu-for-vm.sh" "$TRACE"
    # ... but everything else ran, in order, 14 steps total.
    run _trace_scripts
    expected="install-broker-for-qdwin.sh
install-session-manager.sh
install-polkit-agent-for-vm.sh
install-pwd-for-vm.sh
install-browser-bridge-for-vm.sh
install-portal-backend-for-vm.sh
install-phone-for-vm.sh
install-print-proxy-for-vm.sh
install-snapshots-for-vm.sh
install-tier3-for-vm.sh
install-tier4-host-for-vm.sh
install-tier5-for-vm.sh
install-tier5b-for-vm.sh"
    [ "$output" = "$expected" ]
}

@test "resume: missing/empty state file runs the FULL chain (nothing done yet)" {
    # No state file at all.
    _run_chain 'RESUME=1'
    [ "$status" -eq 0 ]
    run bash -c 'wc -l < "'"$TRACE"'"'
    [ "$(echo "$output" | tr -d ' ')" = "14" ]
    grep -q "install-broker-for-qdwin.sh" "$TRACE"
    grep -q "install-tier5b-for-vm.sh" "$TRACE"
}

@test "resume: all steps already recorded runs NOTHING" {
    mkdir -p "$STATE_DIR"
    bash -c 'source "'"$BOOT"'"; installer_chain_names' \
        > "$STATE_DIR/installer-chain.state"
    _run_chain 'RESUME=1'
    [ "$status" -eq 0 ]
    run bash -c 'wc -l < "'"$TRACE"'"'
    [ "$(echo "$output" | tr -d ' ')" = "0" ]
}

# --- --resume fail-closed on corrupt state ------------------------------

@test "resume corrupt-state: unknown step name in state file is REFUSED (fail-closed)" {
    mkdir -p "$STATE_DIR"
    printf 'broker\nNONSENSE-STEP\npwd\n' > "$STATE_DIR/installer-chain.state"
    _run_chain 'RESUME=1'
    [ "$status" -ne 0 ]
    [[ "$output" == *"corrupt"* ]]
    [[ "$output" == *"NONSENSE-STEP"* ]]
    # Nothing ran.
    run bash -c 'wc -l < "'"$TRACE"'"'
    [ "$(echo "$output" | tr -d ' ')" = "0" ]
}

# --- --rerun-step: runs exactly one step --------------------------------

@test "rerun-step: runs EXACTLY the named step and nothing else" {
    _run_chain 'RERUN_STEP=phone'
    [ "$status" -eq 0 ]
    run _trace_scripts
    [ "$output" = "install-phone-for-vm.sh" ]
    # State file records exactly that one.
    run cat "$STATE_DIR/installer-chain.state"
    [ "$output" = "phone" ]
}

@test "rerun-step: first step in the chain works too" {
    _run_chain 'RERUN_STEP=broker'
    [ "$status" -eq 0 ]
    run _trace_scripts
    [ "$output" = "install-broker-for-qdwin.sh" ]
}

@test "rerun-step: unknown step name is rejected (fail-closed) and runs nothing" {
    _run_chain 'RERUN_STEP=not-a-real-step'
    [ "$status" -ne 0 ]
    [[ "$output" == *"not a known installer-chain step"* ]]
    run bash -c 'wc -l < "'"$TRACE"'"'
    [ "$(echo "$output" | tr -d ' ')" = "0" ]
}

# --- --from-step: runs from a step to the end ---------------------------

@test "from-step: runs from the named step to the end inclusive" {
    _run_chain 'FROM_STEP=snapshots'
    [ "$status" -eq 0 ]
    run _trace_scripts
    expected="install-snapshots-for-vm.sh
install-tier3-for-vm.sh
install-tier4-host-for-vm.sh
install-tier5-for-vm.sh
install-tier5b-for-vm.sh"
    [ "$output" = "$expected" ]
}

@test "from-step: unknown step name is rejected (fail-closed)" {
    _run_chain 'FROM_STEP=bogus'
    [ "$status" -ne 0 ]
    [[ "$output" == *"not a known installer-chain step"* ]]
}

# --- a failed step is NOT recorded; a later --resume re-runs it ----------

@test "failed step is not recorded so --resume re-runs it" {
    # Make the qsu installer FAIL.
    cat > "$FAKE_QD/scripts/install/install-qsu-for-vm.sh" <<EOF
#!/bin/bash
echo "install-qsu-for-vm.sh \$1" >> "$TRACE"
exit 1
EOF
    chmod +x "$FAKE_QD/scripts/install/install-qsu-for-vm.sh"

    # First run (default mode, non-strict): qsu fails but chain continues.
    _run_chain ''
    [ "$status" -eq 0 ]
    # qsu must NOT be recorded as complete.
    ! grep -qx "qsu" "$STATE_DIR/installer-chain.state"
    # but broker..browser-bridge etc ARE recorded.
    grep -qx "broker" "$STATE_DIR/installer-chain.state"

    # Now fix qsu and resume: qsu must run again (it's the first gap).
    cat > "$FAKE_QD/scripts/install/install-qsu-for-vm.sh" <<EOF
#!/bin/bash
echo "install-qsu-for-vm.sh \$1 FIXED" >> "$TRACE"
exit 0
EOF
    chmod +x "$FAKE_QD/scripts/install/install-qsu-for-vm.sh"
    : > "$TRACE"
    _run_chain 'RESUME=1'
    [ "$status" -eq 0 ]
    # The resumed run re-ran qsu (first un-recorded step) ...
    grep -q "install-qsu-for-vm.sh .* FIXED" "$TRACE"
    # ... and did NOT re-run the already-recorded broker.
    ! grep -q "install-broker-for-qdwin.sh" "$TRACE"
}
