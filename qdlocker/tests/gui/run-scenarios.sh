#!/bin/bash
# run-scenarios.sh — orchestrator entry point.
#
# Usage:
#   run-scenarios.sh [--smoke|--all|<NN-name.md> [<NN-name.md> ...]]
#
# In --smoke / --all mode, picks scenarios from this directory and
# emits the per-scenario invocation an LLM runner subagent (or human)
# would execute. The orchestrator pattern in qdwin/tests/gui/AGENTS.md
# expects each runner to take one scenario file + a VMNAME and return
# a PASS/FAIL report (see AGENTS.md §"Report format").
#
# This script is intentionally thin — it does NOT execute the
# scenarios itself. Each scenario is markdown and needs a graphic-
# aware runner to assert against screenshots. The script's job is
# selection + serialization + report aggregation.

set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

usage() {
    cat <<EOF
Usage: $0 [--smoke|--all|<scenario.md> ...]

  --smoke   Run the smoke subset: 01 02 05.
  --all     Run every NN-*.md scenario in this directory.
  <files>   Run the named scenario files (relative to $HERE).

Env:
  VMNAME    libvirt domain name. Defaults to the first running domain
            in qemu:///session.
  RUNNER    Command to execute per scenario. Default: 'echo' (print
            the invocation a runner subagent would execute, then
            stop). Set to e.g. 'bash -x' for a non-agent dry run, or
            to a wrapper that spawns an LLM runner.

Examples:
  $0 --smoke
  VMNAME=qdistro-260515-1200 $0 01-lock-cycle.md
  RUNNER='bash -x' $0 01-lock-cycle.md
EOF
}

SMOKE=(01-lock-cycle.md 02-fprintd-fallback.md 05-keystroke-isolation.md 07-lock-occludes-desktop.md)

scenarios=()
case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --smoke) scenarios=("${SMOKE[@]}") ;;
    --all)
        while IFS= read -r f; do scenarios+=("$f"); done < <(ls 0?-*.md 2>/dev/null | sort)
        ;;
    "")  usage; exit 1 ;;
    *)   scenarios=("$@") ;;
esac

if [ "${#scenarios[@]}" -eq 0 ]; then
    echo "no scenarios selected" >&2
    exit 1
fi

# shellcheck disable=SC1091
source "$HERE/qdlocker-helpers.sh"

if [ -z "${VMNAME:-}" ]; then
    VMNAME=$(virsh -c qemu:///session list --name --state-running | head -1 || true)
fi
if [ -z "${VMNAME:-}" ]; then
    echo "no running libvirt domain; set VMNAME or boot the qdistro VM first" >&2
    exit 2
fi
qdwin_set_vm "$VMNAME"

if ! qdlocker_session_healthy; then
    echo "FAIL: session not healthy on $VMNAME" >&2
    exit 3
fi

RUNNER="${RUNNER:-echo}"
declare -A RESULTS=()
declare -a SCREENSHOTS=()

for s in "${scenarios[@]}"; do
    if [ ! -f "$HERE/$s" ]; then
        echo "SKIP $s (file not found)"
        RESULTS["$s"]=SKIP
        continue
    fi

    echo
    echo "==================================================================="
    echo "Scenario: $s"
    echo "VM:       $VMNAME"
    echo "Runner:   $RUNNER"
    echo "==================================================================="

    # Each scenario is one runner-subagent task. The runner's job:
    #   1. Read $HERE/$s top to bottom.
    #   2. Source qdlocker-helpers.sh and pin VMNAME.
    #   3. Execute Setup → Steps → Asserts → Cleanup verbatim, taking
    #      and inspecting screenshots between steps.
    #   4. Return the report block documented in AGENTS.md.
    #
    # When RUNNER=echo (default), we just emit the invocation. When
    # RUNNER is a real command, we hand it the scenario and the env.
    if [ "$RUNNER" = "echo" ]; then
        cat <<INVOKE
RUNNER INVOCATION (paste to an LLM runner or execute manually):
  Read $HERE/$s top to bottom. Then execute:
    VMNAME=$VMNAME bash -c 'source $HERE/qdlocker-helpers.sh; ...'
  Follow Setup → Steps → Asserts → Cleanup. Return the report block
  from AGENTS.md §Report format. Save screenshots to
  /tmp/${s%.md}-step*.png.
INVOKE
        RESULTS["$s"]=PENDING
    else
        # Best-effort serial run; the runner is responsible for the
        # actual execution semantics.
        VMNAME="$VMNAME" $RUNNER "$HERE/$s"
        rc=$?
        case "$rc" in
            0)  RESULTS["$s"]=PASS ;;
            77) RESULTS["$s"]=SKIP ;;        # bats SKIP convention
            78) RESULTS["$s"]=BLOCKED ;;     # missing precondition (e.g. scenario 05
                                              # needs a qdshell ctrl command that
                                              # isn't implemented yet) — distinct
                                              # from FAIL so CI doesn't red on
                                              # known-incomplete coverage.
            *)  RESULTS["$s"]=FAIL ;;
        esac
    fi

    # Reset between scenarios — every scenario expects locked=False
    # at Setup. Wrap in `runuser -l admin -c` so the admin user's
    # systemd --user manager is reached (vm-exec runs as root by
    # default, and root has no user manager).
    "$QDWIN_VM_EXEC" "$VMNAME" \
        'runuser -l admin -c "systemctl --user restart qdlocker.service"' >/dev/null 2>&1 \
        || true
    sleep 2
done

echo
echo "==================================================================="
echo "Summary"
echo "==================================================================="
for s in "${scenarios[@]}"; do
    printf "%-40s %s\n" "$s" "${RESULTS[$s]:-?}"
done

# Count failures AND pendings — an all-PENDING run with default
# RUNNER=echo means the scenarios were printed but never executed.
# Treat that as a non-zero exit so a CI invocation can't mistake
# "we listed the scenarios" for "we passed them."
fails=0; pendings=0
for r in "${RESULTS[@]}"; do
    case "$r" in
        FAIL) fails=$((fails+1)) ;;
        PENDING) pendings=$((pendings+1)) ;;
    esac
done
if [ "$pendings" -gt 0 ] && [ "$RUNNER" = "echo" ]; then
    echo "NOTE: $pendings scenarios listed but not executed (RUNNER=echo)."
    echo "      Set RUNNER to a real runner to actually run them."
    exit 70  # EX_SOFTWARE — distinct from real failures
fi
exit "$fails"
