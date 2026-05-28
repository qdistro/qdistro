#!/bin/bash
# run-parallel.sh — distribute the phase{6.5,6.6,7}.bats files across
# multiple VMs in parallel. Reduces wall-clock for a full
# 521-pytest + 57-bats validation roughly 3× when the host has
# enough cores/RAM for three concurrent qdwin VMs.
#
# Strategy:
#   - One bats file per VM. We don't sub-divide a single bats file
#     across VMs because the tests inside one file share state
#     (qdshell socket, weston log, /run/user/1000/wayland-1) and the
#     fixture setup at file-scope assumes a single VM. Splitting at
#     file boundaries is the natural seam.
#   - Each worker:
#       1. clone-baseweed.sh --from-baked   (fast, ~1 min)
#       2. fresh-vm-bootstrap.sh            (~3-5 min from baked)
#       3. bats <file>                      (~5-15 min)
#       4. tear down VM unless --keep
#   - The runner streams per-VM logs, prints a final summary, and
#     exits non-zero if any worker fails.
#
# Usage:
#   ./run-parallel.sh                              # all 3 phase.bats files
#   ./run-parallel.sh tiered-isolation.bats                  # subset
#   ./run-parallel.sh --keep compositor-shell.bats         # keep VMs after run
#   ./run-parallel.sh --jobs 2                     # cap concurrency
#   ./run-parallel.sh --no-baked                   # use plain baseweed (slow)
#
# Output:
#   $LOG_DIR/<file>.log   — bats stdout+stderr per file
#   $LOG_DIR/<file>.bootstrap.log — install-deps + fresh-vm-bootstrap
#   $LOG_DIR/summary.txt  — one-line per file: PASS/FAIL + count
#
# One VM per bats file — file-scope is the natural seam since tests inside one file share fixtures.
set -uo pipefail

KEEP=0
NO_BAKED=0
JOBS=0
PREFIX_BASE="parbats"
FILES=()
LOG_DIR_OVERRIDE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --keep)       KEEP=1; shift ;;
        --no-baked)   NO_BAKED=1; shift ;;
        --jobs)       JOBS=$2; shift 2 ;;
        --prefix)     PREFIX_BASE="$2"; shift 2 ;;
        --log-dir)    LOG_DIR_OVERRIDE="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,30p' "$0"; exit 0 ;;
        --*)
            echo "ERROR: unknown flag '$1'" >&2; exit 2 ;;
        *)
            FILES+=("$1"); shift ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSITOR_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$COMPOSITOR_DIR/.." && pwd)"
VM_TOOLS="$REPO_ROOT/scripts/vm"

if [ ${#FILES[@]} -eq 0 ]; then
    FILES=(compositor-shell.bats shell-modules.bats tiered-isolation.bats)
fi

LOG_DIR="${LOG_DIR_OVERRIDE:-/tmp/qdistro-parallel-bats-$(date +%y%m%d-%H%M%S)}"
mkdir -p "$LOG_DIR"

# Default: bound concurrency at min(file-count, 3). The exact upper
# bound is RAM/cpu-driven; 3 qdwin VMs comfortably fit on a 16 GB
# host with 4 cores.
if [ "$JOBS" -le 0 ]; then
    JOBS=${#FILES[@]}
    [ "$JOBS" -gt 3 ] && JOBS=3
fi

if [ ! -x "$VM_TOOLS/clone-baseweed.sh" ]; then
    echo "ERROR: $VM_TOOLS/clone-baseweed.sh not found / not executable" >&2
    exit 1
fi
if ! command -v bats >/dev/null; then
    echo "ERROR: bats not on PATH (zypper install bats)" >&2
    exit 1
fi

# Start a host HTTP server for fresh-vm-bootstrap.sh's wgets. One
# server is shared across all worker VMs. Bind 127.0.0.1; SLIRP NAT
# inside each VM reaches it via 10.0.2.2.
echo "[parallel] starting host HTTP server on 127.0.0.1:8765..."
( cd "$COMPOSITOR_DIR" && exec python3 -m http.server 8765 \
        --bind 127.0.0.1 ) >"$LOG_DIR/http.log" 2>&1 &
HTTP_PID=$!

# Give the server a beat to bind.
sleep 1

cleanup_http() {
    if [ -n "${HTTP_PID:-}" ] && kill -0 "$HTTP_PID" 2>/dev/null; then
        kill "$HTTP_PID" 2>/dev/null || true
    fi
}
trap cleanup_http EXIT

# Per-worker function.
run_one() {
    local file=$1
    local stem=${file%.bats}
    local prefix="${PREFIX_BASE}-${stem}"
    local log="$LOG_DIR/${file}.log"
    local boot="$LOG_DIR/${file}.bootstrap.log"
    local clone_args=("$prefix")
    [ "$NO_BAKED" = 0 ] && clone_args+=(--from-baked)

    {
        echo "=== $(date -Is) [$file] cloning VM ==="
        local vm
        vm=$("$VM_TOOLS/clone-baseweed.sh" "${clone_args[@]}" 2>>"$boot")
        if [ -z "$vm" ]; then
            echo "FAIL: clone-baseweed produced no VM name"
            return 5
        fi
        echo "VM=$vm"
        echo "=== $(date -Is) [$file] running install-deps ==="
        # install-deps is a no-op on baked clones. On --no-baked it
        # is the slow step. Either way we run it for safety.
        "$VM_TOOLS/vm-exec" "$vm" \
            "wget -q -O /root/install-deps.sh http://10.0.2.2:8765/spike-6.5/install-deps.sh && \
             bash /root/install-deps.sh" \
            >>"$boot" 2>&1 || {
                echo "FAIL: install-deps inside $vm"
                tail -20 "$boot"
                return 6
            }
        echo "=== $(date -Is) [$file] running fresh-vm-bootstrap ==="
        "$VM_TOOLS/vm-exec" "$vm" \
            "wget -q -O /root/fresh-vm-bootstrap.sh http://10.0.2.2:8765/spike-6.5/fresh-vm-bootstrap.sh && \
             bash /root/fresh-vm-bootstrap.sh" \
            >>"$boot" 2>&1 || {
                echo "FAIL: fresh-vm-bootstrap inside $vm"
                tail -30 "$boot"
                return 7
            }
        echo "=== $(date -Is) [$file] running bats ==="
        # Each worker uses its own VM via VM_NAME.
        VM_NAME="$vm" bats "$SCRIPT_DIR/$file" >"$log" 2>&1
        local rc=$?
        echo "RC=$rc"
        if [ "$KEEP" -eq 0 ]; then
            virsh -c qemu:///session destroy "$vm" >/dev/null 2>&1 || true
            virsh -c qemu:///session undefine "$vm" --nvram >/dev/null 2>&1 \
                || virsh -c qemu:///session undefine "$vm" >/dev/null 2>&1 \
                || true
            local img="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}/${vm}.qcow2"
            [ -f "$img" ] && rm -f "$img"
        else
            echo "[$file] --keep: VM '$vm' left running with disk at $img"
        fi
        return $rc
    } >>"$boot" 2>&1
}
export -f run_one
export VM_TOOLS SCRIPT_DIR LOG_DIR PREFIX_BASE NO_BAKED KEEP \
       QDWIN_IMG_DIR QDWIN_VM_TEMPLATE

# Spawn workers up to JOBS concurrency.
echo "[parallel] dispatching ${#FILES[@]} files at concurrency=$JOBS"
echo "[parallel] log dir: $LOG_DIR"

declare -A worker_pid
declare -A worker_file
running=0
i=0
results=()
overall=0

while [ "$i" -lt "${#FILES[@]}" ] || [ "$running" -gt 0 ]; do
    while [ "$running" -lt "$JOBS" ] && [ "$i" -lt "${#FILES[@]}" ]; do
        f=${FILES[$i]}
        ( run_one "$f" ) &
        pid=$!
        worker_pid[$pid]=$pid
        worker_file[$pid]=$f
        echo "[parallel] launched [$f] pid=$pid"
        running=$((running + 1))
        i=$((i + 1))
    done

    if [ "$running" -gt 0 ]; then
        # Bash 5.1+: `wait -n -p VAR` exits when any background job
        # completes, writing its PID into VAR and returning the
        # child's exit status (0 on clean exit, non-zero with the
        # child's rc otherwise; 127 only when no jobs to wait for).
        # Older bash without -p falls through to an explicit poll.
        done_pid=""
        wait -n -p done_pid 2>/dev/null
        rc=$?
        if [ -z "$done_pid" ]; then
            # Fallback for older bash: poll all known PIDs.
            for p in "${!worker_pid[@]}"; do
                if ! kill -0 "$p" 2>/dev/null; then
                    wait "$p" 2>/dev/null
                    rc=$?
                    done_pid=$p
                    break
                fi
            done
            if [ -z "$done_pid" ]; then
                sleep 5
                continue
            fi
        fi
        f=${worker_file[$done_pid]}
        unset 'worker_pid[$done_pid]'
        unset 'worker_file[$done_pid]'
        running=$((running - 1))
        if [ "$rc" -eq 0 ]; then
            results+=("PASS  $f")
            echo "[parallel] DONE  PASS  [$f] (rc=0)"
        else
            results+=("FAIL  $f (rc=$rc)")
            echo "[parallel] DONE  FAIL  [$f] (rc=$rc)"
            overall=1
        fi
    fi
done

# Final summary.
{
    echo "# qdistro parallel bats summary  $(date -Is)"
    echo "# log_dir=$LOG_DIR"
    echo
    for r in "${results[@]}"; do
        echo "$r"
    done
} | tee "$LOG_DIR/summary.txt"

cleanup_http
trap - EXIT

exit $overall
