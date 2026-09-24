#!/usr/bin/env bats
#
# `qci <sub> -h|--help` must print usage and exit EXIT_USAGE (2) — the same
# code as top-level `qci --help` — WITHOUT creating a run dir, recording a
# result, or touching libvirt. Before the fix every subcommand called
# init_run first, and then either ran its gate (--help ignored, or taken as a
# bats file / triage run dir) or recorded the flag as an "unknown arg".
#
# Drives the REAL ci/bin/qci with QCI_RUNS_DIR in a temp dir and a stub
# `virsh` first on PATH that logs any call, so a regression cannot reach the
# host's libvirt and is caught by the "virsh never called" assertion.

# Every subcommand main() dispatches, one entry per case arm.
SUBCOMMANDS=(preflight lint selftest image registry-check release-manifest
    bootstrap-release-profile affected edit-guard replay host vm-smoke bats
    gui gui-admin full snapshot-daily mmnet cleanup report triage list-runs)

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    QCI="$REPO_ROOT/ci/bin/qci"
    RUNS="$(mktemp -d)"
    export QCI_RUNS_DIR="$RUNS"
    STUB="$(mktemp -d)"
    VIRSH_LOG="$STUB/virsh.calls"
    printf '#!/bin/sh\necho "$*" >> "%s"\nexit 1\n' "$VIRSH_LOG" > "$STUB/virsh"
    chmod +x "$STUB/virsh"
    export PATH="$STUB:$PATH"
}

teardown() {
    rm -rf "$RUNS" "$STUB"
}

run_count() {
    find "$RUNS" -mindepth 1 -maxdepth 1 | wc -l
}

# Assert one help invocation: usage printed, exit 2, no run dir, no virsh.
assert_help() {
    run timeout 60 "$QCI" "$@"
    if [ "$status" -ne 2 ]; then
        echo "qci $*: status=$status (want 2)"; echo "$output"; return 1
    fi
    if [[ "$output" != *"Usage:"* ]] || [[ "$output" != *"qci list-runs"* ]]; then
        echo "qci $*: usage text missing"; echo "$output"; return 1
    fi
    if [[ "$output" == *"unknown command"* ]]; then
        echo "qci $*: dispatched as unknown command"; echo "$output"; return 1
    fi
    if [ "$(run_count)" -ne 0 ]; then
        echo "qci $*: created a run dir:"; ls "$RUNS"; return 1
    fi
    if [ -e "$VIRSH_LOG" ]; then
        echo "qci $*: called virsh:"; cat "$VIRSH_LOG"; return 1
    fi
}

@test "subcommand --help: every subcommand prints usage, exits 2, no run dir" {
    local sub
    for sub in "${SUBCOMMANDS[@]}"; do
        assert_help "$sub" --help
    done
}

@test "subcommand -h: every subcommand prints usage, exits 2, no run dir" {
    local sub
    for sub in "${SUBCOMMANDS[@]}"; do
        assert_help "$sub" -h
    done
}

@test "subcommand --help after other args still means help (gui/bats/image/full/replay)" {
    assert_help gui --vm some-vm --scenario x.md --help
    assert_help bats --file /dev/null --help
    assert_help image --no-boot --help
    assert_help full --keep-on-fail --help
    assert_help replay some-scenario some-vm --help
    assert_help triage --latest --help
    assert_help affected --changed-from HEAD --help
}

@test "qci help <sub> prints usage, exits 2, no run dir" {
    assert_help help gui
    assert_help help full
}

@test "every command in the Usage: block is a dispatched subcommand" {
    run "$QCI" --help
    local cmds
    cmds=$(printf '%s\n' "$output" | sed -n 's/^  qci \([a-z-]*\).*/\1/p' | sort -u)
    [ -n "$cmds" ]
    local sub
    for sub in $cmds; do
        assert_help "$sub" --help
    done
    # ...and the test's own list matches the Usage: block exactly.
    [ "$cmds" = "$(printf '%s\n' "${SUBCOMMANDS[@]}" | sort -u)" ]
}

@test "unknown command exits 2 with usage and creates no run dir" {
    run "$QCI" not-a-real-gate --help
    [ "$status" -eq 2 ]
    [[ "$output" == *"unknown command: not-a-real-gate"* ]]
    [[ "$output" == *"Usage:"* ]]
    [ "$(run_count)" -eq 0 ]
}

@test "--help after -- is an operand, not a help request (affected path)" {
    # affected without --run only maps paths to gates (no VM); the `--`
    # terminator must pass --help through as a path, so a run dir IS made.
    run timeout 60 "$QCI" affected -- --help
    [ "$status" -eq 0 ]
    [[ "$output" != *"Usage:"* ]]
    [ "$(run_count)" -eq 1 ]
    [ ! -e "$VIRSH_LOG" ]
}

# ---------------------------------------------------------------------------
# Option VALUES that happen to be -h/--help are operands, not help requests
# (review finding: `report --run -h` on a run dir literally named `-h`).
# ---------------------------------------------------------------------------

@test "report --run -h reports on a run dir named -h (real runner)" {
    local work
    work=$(mktemp -d)
    mkdir -- "$work/-h"
    printf 'run_id=x\ngate=lint\n' > "$work/-h/manifest.txt"
    printf 'gate\tsubject\tstatus\texit_code\texit_class\tkind\tlog\tnotes\tcategory\n' > "$work/-h/results.tsv"
    run bash -c 'cd "$1" && timeout 60 "$2" report --run -h' _ "$work" "$QCI"
    local st=$status out=$output
    local made=0
    [ -f "$work/-h/report.md" ] && made=1
    rm -rf "$work"
    echo "status=$st"; echo "$out"
    [ "$st" -eq 0 ]
    [[ "$out" != *"Usage:"* ]]
    [ "$made" -eq 1 ]
}

@test "host-safe value options keep a -h/--help value (real runner, no usage)" {
    # Each of these parses the value, then records its own result without a
    # VM: affected without --run only maps paths; cleanup/edit-guard reject
    # the value as a blocked usage row before touching libvirt.
    local args
    for args in "affected --vm -h -- README.md" \
                "cleanup --age-hours --help" \
                "edit-guard --changed-from -h" \
                "triage --run -h"; do
        # shellcheck disable=SC2086
        run timeout 60 "$QCI" $args
        if [[ "$output" == *"Usage:"* ]]; then
            echo "qci $args: treated the option value as a help request"; echo "$output"; return 1
        fi
        if [ -e "$VIRSH_LOG" ]; then
            echo "qci $args: called virsh"; cat "$VIRSH_LOG"; return 1
        fi
    done
}

# Drive the REAL main() from ci/lib/dispatch.sh with its collaborators
# (init_run, finish_run, usage, the gate functions) replaced by recorders,
# so VM-lane value options can be checked without reaching a VM.
stub_main() {
    bash -c '
        cd "$1"; shift
        EXIT_OK=0 EXIT_USAGE=2 EXPLICIT_VM=""
        # shellcheck disable=SC1090
        . "$REPO_ROOT/ci/lib/dispatch.sh"
        usage() { echo "Usage: (stub)"; }
        init_run() { echo "INIT_RUN $*"; }
        finish_run() { echo "FINISH $1"; exit "$1"; }
        record_blocked() { echo "BLOCKED $*"; }
        for g in gate_preflight gate_host gate_bats gate_gui gate_vm_smoke gate_image gate_snapshot_daily gate_cleanup gate_affected gate_edit_guard; do
            eval "$g() { echo \"GATE $g \$*\"; return 0; }"
        done
        main "$@"
    ' _ "$@"
}

@test "VM-lane value options keep a -h/--help value; a later help option still wins (stubbed gates)" {
    export REPO_ROOT
    local work
    work=$(mktemp -d)
    : > "$work/--help"
    : > "$work/-h"
    run stub_main "$work" bats --file --help
    echo "$output"
    [[ "$output" == *"GATE gate_bats  --help"* ]]
    [[ "$output" != *"Usage:"* ]]
    run stub_main "$work" bats --file -h --vm -h
    echo "$output"
    [[ "$output" == *"GATE gate_bats -h -h"* ]]
    run stub_main "$work" gui --scenario -h
    echo "$output"
    [[ "$output" == *"GATE gate_gui"* ]]
    [[ "$output" != *"Usage:"* ]]
    run stub_main "$work" gui-admin --vm --help
    [[ "$output" == *"GATE gate_gui --help"* ]]
    run stub_main "$work" vm-smoke --vm -h
    [[ "$output" == *"GATE gate_vm_smoke -h"* ]]
    run stub_main "$work" snapshot-daily --name -h --date --help
    [[ "$output" == *"GATE gate_snapshot_daily --help -h"* ]]
    run stub_main "$work" image --root --help
    [[ "$output" == *"GATE gate_image --root --help"* ]]
    # ...but a real help option after a consumed value is still help, and
    # never reaches init_run or a gate.
    local a
    for a in "bats --file --help --help" "gui --scenario -h -h" \
             "image --root -h --help" "report --run -h -h"; do
        # shellcheck disable=SC2086
        run stub_main "$work" $a
        echo "$a -> $status: $output"
        [ "$status" -eq 2 ]
        [[ "$output" == "Usage: (stub)" ]]
    done
    rm -rf "$work"
}

# ---------------------------------------------------------------------------
# Drift: derive the REAL dispatch case arms and the REAL value-taking options
# from the parsers, and compare them with the registration tables.
# ---------------------------------------------------------------------------

# Print "<arm-label>" lines for each arm of main()'s final dispatch case.
dispatch_arms() {
    awk '
        /^main\(\) \{/ { inmain=1 }
        inmain && /^    local rc=\$EXIT_OK/ { armed=1; next }
        armed && !incase && /^    case "\$cmd" in/ { incase=1; next }
        incase && /^    esac/ { exit }
        incase && /^        [a-z][a-z|-]*\)/ {
            lbl=$1; sub(/\).*/, "", lbl); n=split(lbl, parts, "|")
            for (i=1; i<=n; i++) print parts[i]
        }
    ' "$REPO_ROOT/ci/lib/dispatch.sh"
}

# Print "<cmd> <--opt>" for every option arm that consumes a value, from
# main()'s arm or from the first `while [ $# -gt 0 ]` arg loop of a gate
# function the arm passes "$@" to. An option arm is any case label starting
# with `-`; its body runs to the first `;;`. It consumes a value when that body
# does `shift` or reads `$2`/`${2`, whatever the order (`--x) shift; v=$1`,
# `--x) v=${2:-}; shift`, a two-line `--x)` / `shift`). A label this parser
# cannot map to one exact token (`--a|-a)`, `--x=*)`, `-v`) is printed as
# "UNSUPPORTED <cmd> <label>" so the drift test fails instead of missing it.
# `--)` and `-*)` are the terminator / unknown-flag arms and are ignored.
parsed_value_opts() {
    awk -v libdir="$REPO_ROOT/ci/lib/gates" '
        function scan_block(n,    i, j, lbl, body, k, done_arm) {
            for (i=1; i<=n; i++) {
                if (buf[i] !~ /^ *-[^ ]*\)/) continue
                lbl=buf[i]; sub(/^ */, "", lbl); sub(/\).*/, "", lbl)
                if (lbl == "--" || lbl == "-*") continue
                body=buf[i]; sub(/^ *[^)]*\)/, "", body)
                j=i
                while (body !~ /;;/ && j < n) { j++; body=body "\n" buf[j] }
                if (lbl !~ /^--[a-z][a-z-]*$/) {
                    for (k=1; k<=ncur; k++) print "UNSUPPORTED", cur[k], lbl
                    continue
                }
                if (body ~ /(^|[^a-z_])shift([^a-z_]|$)/ || body ~ /\$2|\$\{2/)
                    for (k=1; k<=ncur; k++) print cur[k], lbl
            }
        }
        function gate_block(g,    cmd, l, inf, inloop, n) {
            cmd="cat " libdir "/*.sh"; inf=0; inloop=0; n=0
            while ((cmd | getline l) > 0) {
                if (l ~ ("^" g "\\(\\) \\{")) { inf=1; continue }
                if (inf && l ~ /^}/) inf=0
                if (inf && !inloop && l ~ /while \[ \$# -gt 0 \]/) { inloop=1; continue }
                if (inloop && l ~ /^    done/) { inloop=0; inf=0 }
                if (inloop) buf[++n]=l
            }
            close(cmd)
            return n
        }
        /^main\(\) \{/ { inmain=1 }
        inmain && /^    local rc=\$EXIT_OK/ { armed=1; next }
        armed && !incase && /^    case "\$cmd" in/ { incase=1; next }
        incase && /^    esac/ { exit }
        incase { lines[++nl]=$0 }
        END {
            ncur=0; n=0
            for (i=1; i<=nl+1; i++) {
                if (i > nl || lines[i] ~ /^        [a-z][a-z|-]*\)/) {
                    if (ncur) scan_block(n)
                    for (gi=1; gi<=ngates; gi++) { m=gate_block(gates[gi]); scan_block(m) }
                    if (i > nl) break
                    lbl=lines[i]; sub(/^ */, "", lbl); sub(/\).*/, "", lbl)
                    ncur=split(lbl, cur, "|"); n=0; ngates=0
                    continue
                }
                buf[++n]=lines[i]
                if (match(lines[i], /gate_[a-z_]+ "\$@"/)) {
                    g=substr(lines[i], RSTART, RLENGTH); sub(/ .*/, "", g)
                    gates[++ngates]=g
                }
            }
        }
    ' "$REPO_ROOT/ci/lib/dispatch.sh" | sort -u
}

@test "drift: dispatch case arms == QCI_COMMANDS == Usage: block" {
    local arms reg usage_cmds
    arms=$(dispatch_arms | sort -u)
    [ "$(printf '%s\n' "$arms" | wc -l)" -ge 20 ]
    reg=$(bash -c '. "$1"; printf "%s\n" $QCI_COMMANDS' _ "$REPO_ROOT/ci/lib/dispatch.sh" | sort -u)
    usage_cmds=$("$QCI" --help | sed -n 's/^  qci \([a-z-]*\).*/\1/p' | sort -u)
    diff <(echo "$arms") <(echo "$reg")
    diff <(echo "$arms") <(echo "$usage_cmds")
}

@test "drift: qci_value_opts == every value-taking option in the real parsers" {
    local parsed table
    parsed=$(parsed_value_opts)
    # Sanity: the extractor sees both one-line and two-line shift forms and
    # options parsed inside gate functions.
    [[ "$parsed" == *"bats --file"* ]]
    [[ "$parsed" == *"edit-guard --changed-from"* ]]
    [[ "$parsed" == *"image --root"* ]]
    [[ "$parsed" == *"cleanup --age-hours"* ]]
    # Boolean options must NOT be reported as value options.
    [[ "$parsed" != *"affected --run"* ]]
    [[ "$parsed" != *"gui --skip-qdwin"* ]]
    [[ "$parsed" != *"image --no-boot"* ]]
    [[ "$parsed" != *UNSUPPORTED* ]]
    table=$(bash -c '. "$1"; for c in $QCI_COMMANDS; do for o in $(qci_value_opts "$c"); do echo "$c $o"; done; done' _ "$REPO_ROOT/ci/lib/dispatch.sh" | sort -u)
    diff <(echo "$parsed") <(echo "$table")
}

@test "an empty or space-joined argument is not a value option; the help after it wins (stubbed gates)" {
    export REPO_ROOT
    local work
    work=$(mktemp -d)
    # Commands with NO value options: "" must not match the empty table.
    run stub_main "$work" preflight "" --help
    echo "preflight '' --help -> $status: $output"
    [ "$status" -eq 2 ]
    [ "$output" = "Usage: (stub)" ]
    run stub_main "$work" host "" -h
    echo "host '' -h -> $status: $output"
    [ "$status" -eq 2 ]
    [ "$output" = "Usage: (stub)" ]
    # A command WITH value options: "" and one argv element spelling two
    # table entries are neither of them.
    run stub_main "$work" bats "" --help
    echo "bats '' --help -> $status: $output"
    [ "$status" -eq 2 ]
    [ "$output" = "Usage: (stub)" ]
    run stub_main "$work" bats "--vm --file" --help
    echo "bats '--vm --file' --help -> $status: $output"
    [ "$status" -eq 2 ]
    [ "$output" = "Usage: (stub)" ]
    run stub_main "$work" gui "--vm --scenario" -h
    echo "gui '--vm --scenario' -h -> $status: $output"
    [ "$status" -eq 2 ]
    [ "$output" = "Usage: (stub)" ]
    rm -rf "$work"
}

@test "list-runs '' --help prints usage through the real runner" {
    run timeout 60 "$QCI" list-runs "" --help
    [ "$status" -eq 2 ]
    [[ "$output" == *"Usage:"* ]]
    [ "$(run_count)" -eq 0 ]
}
