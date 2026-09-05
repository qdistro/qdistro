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
# Also the end-of-run completeness check (iso2 02 F1, todo/iso/14 Phase D):
# recorded steps vs the chain in both directions (missing AND unexpected,
# as sets), fatal in hardened profiles / strict, warn in dev, report-only
# for scoped runs.
#
# Run: bats tests/integration/vm/bootstrap-installer-resume.bats

setup() {
    REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" \
                    rev-parse --show-toplevel 2>/dev/null)"
    BOOT="$REPO_ROOT/scripts/install/qdistro-bootstrap.sh"

    # Fake qdistro source tree: $FAKE_QD acts as $REPO_ROOT/qdistro. Install a
    # stub for every chain installer that appends "<basename> <srcdir>"
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
        install-sdk-for-vm.sh
        install-broker-for-qdwin.sh
        install-session-manager.sh
        install-user-relay-for-vm.sh
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
# Args: extra shell to set the rerun-mode globals (e.g. 'RESUME=1', or
# 'QDISTRO_PROFILE=dev'). The profile is the bootstrap's default,
# daily-driver (hardened), unless the caller sets it; STRICT is unset.
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

# --- baseline: release-profile chain runs in order, state records each step ----

@test "full chain: all release-profile steps run in order and each is recorded in state" {
    _run_chain ''
    [ "$status" -eq 0 ]
    # Exact ordered set of scripts that ran.
    run _trace_scripts
    [ "$status" -eq 0 ]
    expected="install-sdk-for-vm.sh
install-broker-for-qdwin.sh
install-session-manager.sh
install-user-relay-for-vm.sh
install-polkit-agent-for-vm.sh
install-pwd-for-vm.sh
install-qsu-for-vm.sh
install-browser-bridge-for-vm.sh
install-portal-backend-for-vm.sh
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
    state_expected="sdk
broker
session-manager
user-relay
polkit
pwd
qsu
browser-bridge
portal-backend
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
    grep -qx "install-sdk-for-vm.sh $FAKE_QD/sdk/qdistro_app" "$TRACE"
    grep -qx "install-session-manager.sh $FAKE_QD/session_manager" "$TRACE"
    grep -qx "install-user-relay-for-vm.sh $FAKE_QD/user_relay" "$TRACE"
    # portal-backend / tier3 / tier4-host / tier5 / tier5b -> bare QD
    grep -qx "install-portal-backend-for-vm.sh $FAKE_QD" "$TRACE"
    grep -qx "install-tier3-for-vm.sh $FAKE_QD" "$TRACE"
}

# --- --resume: skips the recorded-complete prefix, runs only the remainder --

@test "resume: with broker..qsu recorded, runs ONLY sdk plus browser-bridge..tier5b" {
    mkdir -p "$STATE_DIR"
    printf 'broker\nsession-manager\nuser-relay\npolkit\npwd\nqsu\n' \
        > "$STATE_DIR/installer-chain.state"
    _run_chain 'RESUME=1'
    [ "$status" -eq 0 ]
    run _trace_scripts
    expected="install-sdk-for-vm.sh
install-browser-bridge-for-vm.sh
install-portal-backend-for-vm.sh
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
    # ... but every other release-profile step ran, in order.
    run _trace_scripts
    expected="install-sdk-for-vm.sh
install-broker-for-qdwin.sh
install-session-manager.sh
install-user-relay-for-vm.sh
install-polkit-agent-for-vm.sh
install-pwd-for-vm.sh
install-browser-bridge-for-vm.sh
install-portal-backend-for-vm.sh
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
    [ "$(echo "$output" | tr -d ' ')" = "15" ]
    grep -q "install-sdk-for-vm.sh" "$TRACE"
    grep -q "install-broker-for-qdwin.sh" "$TRACE"
    grep -q "install-tier5b-for-vm.sh" "$TRACE"
}

@test "resume: all steps already recorded runs NOTHING" {
    mkdir -p "$STATE_DIR"
    # every step the default (daily-driver) profile expects -- phone is
    # dev-only and, if recorded here, would rightly be an unexpected record
    bash -c 'source "'"$BOOT"'"; resolve_profile >/dev/null; chain_expected_names' \
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

@test "rerun-step: runs EXACTLY the named release-profile step and nothing else" {
    _run_chain 'RERUN_STEP=print'
    [ "$status" -eq 0 ]
    run _trace_scripts
    [ "$output" = "install-print-proxy-for-vm.sh" ]
    # State file records exactly that one.
    run cat "$STATE_DIR/installer-chain.state"
    [ "$output" = "print" ]
}

@test "rerun-step: first step in the chain works too" {
    _run_chain 'RERUN_STEP=sdk'
    [ "$status" -eq 0 ]
    run _trace_scripts
    [ "$output" = "install-sdk-for-vm.sh" ]
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

    # First run (default mode, non-strict, hardened profile): qsu fails, the
    # chain CONTINUES past it (every later step still runs, so --resume has
    # exactly one gap to fill) -- and the run then exits nonzero from the
    # end-of-run completeness check, naming the gap.
    _run_chain ''
    [ "$status" -ne 0 ]
    [[ "$output" == *"installer chain INCOMPLETE in 'daily-driver' profile"* ]]
    [[ "$output" == *"not recorded as installed: qsu."* ]]
    grep -q "install-tier5b-for-vm.sh" "$TRACE"
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
    [[ "$output" != *"INCOMPLETE"* ]]
    # The resumed run re-ran qsu (first un-recorded step) ...
    grep -q "install-qsu-for-vm.sh .* FIXED" "$TRACE"
    # ... and did NOT re-run the already-recorded broker.
    ! grep -q "install-broker-for-qdwin.sh" "$TRACE"
}

# --- end-of-run completeness check (iso2 02 F1) ---------------------------

_break_step() {
    cat > "$FAKE_QD/scripts/install/$1" <<EOF
#!/bin/bash
echo "$1 \$1" >> "$TRACE"
exit 1
EOF
    chmod +x "$FAKE_QD/scripts/install/$1"
}

@test "completeness: a clean hardened run reports the chain complete (phone is not a gap)" {
    _run_chain ''
    [ "$status" -eq 0 ]
    # 16 steps, phone skipped as dev-only, so 15 expected and 15 recorded.
    [ "$(grep -c . "$STATE_DIR/installer-chain.state")" -eq 15 ]
    ! grep -qx phone "$STATE_DIR/installer-chain.state"
    ! grep -q "install-phone-for-vm.sh" "$TRACE"
}

@test "completeness: dev profile -- a failed step is a WARN, the run exits 0, and phone is expected" {
    _break_step install-pwd-for-vm.sh
    _run_chain 'QDISTRO_PROFILE=dev'
    [ "$status" -eq 0 ]
    [[ "$output" == *"installer chain INCOMPLETE (dev profile continues): not recorded as installed: pwd."* ]]
    grep -q "install-phone-for-vm.sh" "$TRACE"
    grep -qx phone "$STATE_DIR/installer-chain.state"
    ! grep -qx pwd "$STATE_DIR/installer-chain.state"
}

@test "completeness: release profile -- a MISSING installer is a gap and the run exits nonzero" {
    rm "$FAKE_QD/scripts/install/install-tier4-host-for-vm.sh"
    _run_chain 'QDISTRO_PROFILE=release'
    [ "$status" -ne 0 ]
    [[ "$output" == *"installer not found or not executable"* ]]
    [[ "$output" == *"installer chain INCOMPLETE in 'release' profile: not recorded as installed: tier4-host."* ]]
}

@test "completeness: two gaps are both named" {
    _break_step install-pwd-for-vm.sh
    _break_step install-tier3-for-vm.sh
    _run_chain ''
    [ "$status" -ne 0 ]
    [[ "$output" == *"not recorded as installed: pwd tier3."* ]]
}

@test "completeness: a scoped run (--rerun-step) on a partial machine reports, does not die" {
    _run_chain 'RERUN_STEP=print'
    [ "$status" -eq 0 ]
    [[ "$output" == *"installer chain not complete after a scoped run"* ]]
    [[ "$output" != *"INCOMPLETE in"* ]]
    # --from-step likewise
    : > "$TRACE"; rm -f "$STATE_DIR/installer-chain.state"
    _run_chain 'FROM_STEP=tier5'
    [ "$status" -eq 0 ]
    [[ "$output" == *"installer chain not complete after a scoped run"* ]]
}

@test "completeness: strict -- a failed step aborts at once; nothing after it runs" {
    _break_step install-qsu-for-vm.sh
    _run_chain 'STRICT=1'
    [ "$status" -ne 0 ]
    grep -q "install-qsu-for-vm.sh" "$TRACE"
    ! grep -q "install-browser-bridge-for-vm.sh" "$TRACE"
    ! grep -qx qsu "$STATE_DIR/installer-chain.state"
}

@test "completeness: strict in DEV (the image build) is fatal too -- the gap can never ship" {
    # image/config.sh exports QDISTRO_STRICT=1 with QDISTRO_PROFILE=dev.
    # Break the record instead of a step: an unwritable state dir means a
    # succeeded step cannot be recorded (chain_state_record warns and
    # returns 0), so the ONLY thing standing between that and a green build
    # is the completeness check.
    # (chain_state_record's own `install -d -m 0755` would re-open a
    # read-only state dir, so the dir is made uncreatable instead.)
    mkdir -p "$BATS_TEST_TMPDIR/ro"; chmod 0555 "$BATS_TEST_TMPDIR/ro"
    _run_chain 'QDISTRO_PROFILE=dev; STRICT=1; QDISTRO_STATE_DIR="'"$BATS_TEST_TMPDIR"'/ro/state"; CHAIN_STATE_FILE="$QDISTRO_STATE_DIR/installer-chain.state"'
    chmod 0755 "$BATS_TEST_TMPDIR/ro"
    [ "$status" -ne 0 ]
    [[ "$output" == *"installer chain INCOMPLETE in 'dev' profile"* ]]
    # every step ran (the failure is in the record, not the steps)
    grep -q "install-tier5b-for-vm.sh" "$TRACE"
}

@test "completeness: hardened -- an unwritable state dir fails the run rather than exit 0" {
    mkdir -p "$BATS_TEST_TMPDIR/ro"; chmod 0555 "$BATS_TEST_TMPDIR/ro"
    _run_chain 'QDISTRO_STATE_DIR="'"$BATS_TEST_TMPDIR"'/ro/state"; CHAIN_STATE_FILE="$QDISTRO_STATE_DIR/installer-chain.state"'
    chmod 0755 "$BATS_TEST_TMPDIR/ro"
    [ "$status" -ne 0 ]
    [[ "$output" == *"not recorded"* ]]
    [[ "$output" == *"INCOMPLETE in 'daily-driver'"* ]]
}

# --- completeness, the other direction: RECORDED but not expected -------------
# A full run never removes what it does not install, so a record left behind
# by an earlier install (a dev-only step on a machine now installed as
# release, or a step this bootstrap no longer knows) must be a gap too, or a
# release run would exit 0 "15 of 15" with phone still on disk.

_seed_state() {
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$@" > "$STATE_DIR/installer-chain.state"
}

@test "completeness: release -- phone recorded by an earlier dev install is UNEXPECTED and fatal" {
    _seed_state phone
    _run_chain 'QDISTRO_PROFILE=release'
    [ "$status" -ne 0 ]
    [[ "$output" == *"INCOMPLETE in 'release' profile"* ]]
    [[ "$output" == *"recorded but not part of the 'release' chain: phone(dev-only)"* ]]
    [[ "$output" == *"remove what its scripts/install/install-<step>-for-vm.sh laid down"* ]]
    [[ "$output" != *"installer chain complete"* ]]
    # every release step still ran and was recorded; phone was not run. The
    # full run reset the record, so the file now describes this run only
    # (the operator is told about the phone artifacts in the message).
    [ "$(_trace_scripts | wc -l)" -eq 15 ]
    ! grep -q "install-phone-for-vm.sh" "$TRACE"
    ! grep -qx phone "$STATE_DIR/installer-chain.state"
    [ "$(grep -c . "$STATE_DIR/installer-chain.state")" -eq 15 ]
}

# --- completeness judges THIS run: a full run resets a stale record -----------

@test "completeness: full run -- a stale record does not mask a step that fails NOW (hardened)" {
    # A daily-driver machine installed once, complete. Re-run the full
    # chain with pwd now broken: the old 'pwd' line must not count.
    _run_chain ''
    [ "$status" -eq 0 ]
    [ "$(grep -c . "$STATE_DIR/installer-chain.state")" -eq 15 ]
    : > "$TRACE"
    _break_step install-pwd-for-vm.sh
    _run_chain ''
    [ "$status" -ne 0 ]
    [[ "$output" == *"resetting the record"* ]] || true   # log() is silenced in _run_chain
    [[ "$output" == *"INCOMPLETE in 'daily-driver' profile: not recorded as installed: pwd."* ]]
    ! grep -qx pwd "$STATE_DIR/installer-chain.state"
    [ "$(grep -c . "$STATE_DIR/installer-chain.state")" -eq 14 ]
    # ...and --resume now re-runs exactly pwd
    cat > "$FAKE_QD/scripts/install/install-pwd-for-vm.sh" <<EOF
#!/bin/bash
echo "install-pwd-for-vm.sh \$1" >> "$TRACE"
exit 0
EOF
    : > "$TRACE"
    _run_chain 'RESUME=1'
    [ "$status" -eq 0 ]
    [ "$(_trace_scripts)" = "install-pwd-for-vm.sh" ]
}

@test "completeness: full run -- a clean re-run leaves exactly this run's record (no accumulation)" {
    _seed_state retired-thing
    _run_chain 'QDISTRO_PROFILE=dev'
    [ "$status" -eq 0 ]
    [[ "$output" == *"retired-thing(unknown)"* ]]
    ! grep -qx retired-thing "$STATE_DIR/installer-chain.state"
    [ "$(grep -c . "$STATE_DIR/installer-chain.state")" -eq 16 ]
}

@test "completeness: a record write failure is named as such, not as a failed installer" {
    mkdir -p "$BATS_TEST_TMPDIR/ro"; chmod 0555 "$BATS_TEST_TMPDIR/ro"
    _run_chain 'QDISTRO_PROFILE=dev; STRICT=1; QDISTRO_STATE_DIR="'"$BATS_TEST_TMPDIR"'/ro/state"; CHAIN_STATE_FILE="$QDISTRO_STATE_DIR/installer-chain.state"'
    chmod 0755 "$BATS_TEST_TMPDIR/ro"
    [ "$status" -ne 0 ]
    [[ "$output" == *"ran OK but the record could not be written: sdk broker"* ]]
    [[ "$output" == *"fix $BATS_TEST_TMPDIR/ro/state"* ]]
}

@test "completeness: hardened -- a retired step name in the record is fatal in a FULL run (--resume refuses it up front)" {
    _seed_state retired-thing
    _run_chain ''
    [ "$status" -ne 0 ]
    [[ "$output" == *"INCOMPLETE in 'daily-driver' profile"* ]]
    [[ "$output" == *"retired-thing(unknown)"* ]]
    [[ "$output" != *"not recorded as installed"* ]]
}

@test "completeness: missing and unexpected are both named in one message" {
    _seed_state phone
    _break_step install-pwd-for-vm.sh
    _run_chain 'QDISTRO_PROFILE=release'
    [ "$status" -ne 0 ]
    [[ "$output" == *"not recorded as installed: pwd. recorded but not part of the 'release' chain: phone(dev-only)"* ]]
}

@test "completeness: dev -- an unexpected record is a WARN and the run exits 0" {
    _seed_state retired-thing
    _run_chain 'QDISTRO_PROFILE=dev'
    [ "$status" -eq 0 ]
    [[ "$output" == *"WARN: installer chain INCOMPLETE (dev profile continues): recorded but not part of the 'dev' chain: retired-thing(unknown)"* ]]
    # phone is expected in dev, so it is not unexpected
    [[ "$output" != *"phone(dev-only)"* ]]
}

@test "completeness: a scoped run reports an unexpected record and does not die" {
    _seed_state phone
    _run_chain 'QDISTRO_PROFILE=release; RERUN_STEP=print'
    [ "$status" -eq 0 ]
    [[ "$output" == *"installer chain not complete after a scoped run"* ]]
    [[ "$output" == *"phone(dev-only)"* ]]
    [[ "$output" != *"INCOMPLETE in"* ]]
}

@test "completeness: a reordered but equal record is complete (--resume appends in run order)" {
    # Sets, not order: a machine recovered by --resume records the retried
    # step last. Seed everything except pwd, resume, and expect "complete".
    local names
    names="$(bash -c '. "$1"; QDISTRO_PROFILE=release; resolve_profile >/dev/null; chain_expected_names' _ "$BOOT" | grep -vx pwd)"
    # shellcheck disable=SC2086
    _seed_state $names
    _run_chain 'QDISTRO_PROFILE=release; RESUME=1; log() { echo "LOG: $*"; }'
    [ "$status" -eq 0 ]
    [[ "$output" == *"installer chain complete: 15 of 15 expected steps recorded, nothing unexpected"* ]]
    [ "$(tail -1 "$STATE_DIR/installer-chain.state")" = pwd ]
    [ "$(_trace_scripts)" = "install-pwd-for-vm.sh" ]
}
