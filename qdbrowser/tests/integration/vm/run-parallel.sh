#!/bin/bash
# qdbrowser parallel VM bats runner. Mirrors qdistro/tests/integration/vm/run-parallel.sh:
# one bats file per VM, parallel up to --jobs N.
#
# Requires:
#   - libvirt with a baseweed-qdbrowser template
#   - $QDWIN_VM_TEMPLATE pointing at it
#   - qdistro's clone-baseweed.sh + fresh-vm-bootstrap.sh in PATH or
#     at <monorepo root>/scripts/vm.
#
# Usage:
#   ./run-parallel.sh                          # all .bats files in this dir
#   ./run-parallel.sh qdbrowser-smoke.bats     # subset
#   ./run-parallel.sh --jobs 2                 # cap concurrency
#   ./run-parallel.sh --keep                   # keep VMs after run

set -uo pipefail

KEEP=0
JOBS=0
LOG_DIR_OVERRIDE=""
FILES=()

while [ $# -gt 0 ]; do
    case "$1" in
        --keep)       KEEP=1; shift ;;
        --jobs)       JOBS=$2; shift 2 ;;
        --log-dir)    LOG_DIR_OVERRIDE="$2"; shift 2 ;;
        -h|--help)
            sed -n '1,30p' "$0"
            exit 0
            ;;
        *)            FILES+=("$1"); shift ;;
    esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null || cd "$HERE/../../.." && pwd)"
QDISTRO_SCRIPTS="${QDISTRO_SCRIPTS:-$REPO_ROOT/scripts/vm}"   # REPO_ROOT = monorepo root
LOG_DIR="${LOG_DIR_OVERRIDE:-/tmp/qdbrowser-bats-$$}"
mkdir -p "$LOG_DIR"

if [[ ${#FILES[@]} -eq 0 ]]; then
    mapfile -t FILES < <(cd "$HERE" && ls *.bats 2>/dev/null)
fi
if [[ ${#FILES[@]} -eq 0 ]]; then
    echo "No bats files to run." >&2
    exit 2
fi

if [[ $JOBS -le 0 ]]; then
    JOBS=$(nproc 2>/dev/null || echo 2)
    [[ $JOBS -gt 4 ]] && JOBS=4
fi

echo "Files:    ${FILES[*]}"
echo "Jobs:     $JOBS"
echo "Log dir:  $LOG_DIR"
echo "QDistro:  $QDISTRO_SCRIPTS"
echo

run_one() {
    local bats_file="$1"
    local stem="${bats_file%.bats}"
    local vm_name="qdb-${stem}-$$"
    local log="$LOG_DIR/$stem.log"
    local boot="$LOG_DIR/$stem.bootstrap.log"

    echo "[$stem] cloning VM $vm_name"
    if ! "$QDISTRO_SCRIPTS/clone-baseweed.sh" --from-baked \
            --name "$vm_name" >"$boot" 2>&1; then
        echo "[$stem] FAIL clone"
        return 1
    fi
    if ! "$QDISTRO_SCRIPTS/fresh-vm-bootstrap.sh" "$vm_name" \
            >>"$boot" 2>&1; then
        echo "[$stem] FAIL bootstrap"
        return 1
    fi

    VM_NAME="$vm_name" bats "$HERE/$bats_file" >"$log" 2>&1
    local rc=$?
    if [[ $rc -eq 0 ]]; then
        echo "[$stem] PASS"
    else
        echo "[$stem] FAIL (rc=$rc) — see $log"
    fi

    if [[ $KEEP -eq 0 ]]; then
        "$QDISTRO_SCRIPTS/cleanup-vm.sh" "$vm_name" >/dev/null 2>&1
    fi
    return $rc
}

export -f run_one
export HERE LOG_DIR QDISTRO_SCRIPTS KEEP

printf '%s\n' "${FILES[@]}" \
    | xargs -P "$JOBS" -I{} bash -c 'run_one "$@"' _ {}

fail=0
for f in "${FILES[@]}"; do
    stem="${f%.bats}"
    if ! grep -q '^ok ' "$LOG_DIR/$stem.log" 2>/dev/null; then
        fail=$((fail+1))
    fi
done

echo
echo "Summary: $((${#FILES[@]} - fail)) / ${#FILES[@]} files passed"
exit $((fail > 0 ? 1 : 0))
