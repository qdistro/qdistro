#!/bin/bash
# s05 — net-VM resource budget probe (Probe 14).
#
# Measures honest resource numbers for the "stationary 64GB workstation"
# net-VM sizing claim. Boots a live OpenWrt 24.10.7 net-VM, collects:
#   - boot_secs         (from harness JSON, two cold boots)
#   - guest idle RAM    (/proc/meminfo: MemTotal/MemFree/MemAvailable/Cached/Slab)
#   - guest process count and top-6 VSZ/CMD consumers (from top -bn1)
#   - host qemu RSS     (ps -o rss=,vsz= against the qemu pid from JSON)
#   - guest idle loadavg (CAVEATED: siblings may share host CPUs)
#
# Asserts BOUNDS tied to the measured baseline (so a real regression fails, not
# just a fire):
#   boot_secs      < 60 s   (measured ~25 s: boot to command-ready incl. first-boot)
#   MemAvailable   > 100 MB (measured ~142 MB idle)
#   host qemu RSS  in (50 MB, 300 MB)  (measured ~205 MB; the floor also catches
#                                       a dead qemu — ps yielding "0 0")
#
# NOT MEASURED HERE (explicitly refused):
#   - steady-state CPU under load  — sibling probe VMs contaminate the host
#   - WAN throughput / latency     — slirp is not representative of real NICs
#
# Notes on BusyBox ps: OpenWrt 24.10 BusyBox ps does NOT support -o; only
#   "ps -w" (wide output) and "ps w" work. RSS is not exposed by BusyBox ps.
#   We use "top -bn1" (which shows %VSZ and %CPU) for the process breakdown.
#
# Boots with --no-provision: this probe measures OS/qemu footprint and never
#   uses the rpcd control plane, so it skips the login setup entirely.
#
# Usage:
#   NETVM_BASE=~/.local/share/libvirt/images/qdistro-netvm-base.qcow2 \
#   NETVM_HTTP_PORT=8091 \
#   NETVM_RUNDIR=/tmp/netvm-p14 \
#   bash tests/integration/vm/s05-netvm-resource-budget.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION_PY="$SCRIPT_DIR/lib/netvm_session.py"

NETVM_BASE="${NETVM_BASE:-$HOME/.local/share/libvirt/images/qdistro-netvm-base.qcow2}"
NETVM_HTTP_PORT="${NETVM_HTTP_PORT:-8091}"
NETVM_RUNDIR="${NETVM_RUNDIR:-/tmp/netvm-p14}"
NETVM_NAME="${NETVM_NAME:-p14}"
NETVM_MEM="${NETVM_MEM:-256}"

pass=0; fail=0
ok()  { echo "PASS: $1"; pass=$((pass+1)); }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }

# Always tear down the VM on exit.
_cleanup() {
    echo "--- cleanup: stopping VM $NETVM_NAME (rundir=$NETVM_RUNDIR) ---"
    python3 "$SESSION_PY" down --rundir "$NETVM_RUNDIR" --purge 2>/dev/null || true
}
trap _cleanup EXIT

# Run a guest console command; print stdout.
_run() {
    python3 "$SESSION_PY" run --rundir "$NETVM_RUNDIR" --timeout "${2:-30}" "$1"
}

# ---------------------------------------------------------------------------
echo "=== s05: net-VM resource budget ==="
echo "Base image : $NETVM_BASE"
echo "HTTP port  : $NETVM_HTTP_PORT"
echo "Run dir    : $NETVM_RUNDIR"
echo "Guest RAM  : ${NETVM_MEM} MB"
echo ""

# ---------------------------------------------------------------------------
# 1. Boot run 1
# ---------------------------------------------------------------------------
echo "--- Boot run 1 ---"
rm -rf "$NETVM_RUNDIR"

BOOT1_JSON="$(python3 "$SESSION_PY" up \
    --name "$NETVM_NAME" \
    --base "$NETVM_BASE" \
    --http-port "$NETVM_HTTP_PORT" \
    --rundir "$NETVM_RUNDIR" \
    --mem "$NETVM_MEM" \
    --no-provision)"

echo "Harness JSON: $BOOT1_JSON"
BOOT1_SECS="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["boot_secs"])' "$BOOT1_JSON")"
QEMU_PID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["pid"])' "$BOOT1_JSON")"
echo "Boot run 1: ${BOOT1_SECS}s  (qemu pid $QEMU_PID)"

if python3 -c "import sys; sys.exit(0 if float('$BOOT1_SECS') < 60 else 1)"; then
    ok "boot_secs run1=${BOOT1_SECS} < 60"
else
    bad "boot_secs run1=${BOOT1_SECS} >= 60"
fi

# No provisioning: this probe measures the OS/qemu footprint and never touches
# the rpcd control plane, so it boots with --no-provision and skips the login
# setup entirely (one less thing to flake, no error-swallowing).

# ---------------------------------------------------------------------------
# 2. Settle, collect RAM and process metrics
# ---------------------------------------------------------------------------
echo ""
echo "--- Settling 10s before RAM measurement ---"
sleep 10

echo "--- Guest /proc/meminfo ---"
MEMINFO="$(_run "cat /proc/meminfo" 30)"
echo "$MEMINFO"

mem_kb() { echo "$MEMINFO" | grep "^$1:" | awk '{print $2}'; }
MEM_TOTAL_KB="$(mem_kb MemTotal)"
MEM_FREE_KB="$(mem_kb MemFree)"
MEM_AVAIL_KB="$(mem_kb MemAvailable)"
MEM_CACHED_KB="$(mem_kb Cached)"
MEM_SLAB_KB="$(mem_kb Slab)"

to_mb() { python3 -c "print(round(int('$1')/1024, 1))"; }
MEM_TOTAL_MB="$(to_mb "$MEM_TOTAL_KB")"
MEM_FREE_MB="$(to_mb "$MEM_FREE_KB")"
MEM_AVAIL_MB="$(to_mb "$MEM_AVAIL_KB")"
MEM_CACHED_MB="$(to_mb "$MEM_CACHED_KB")"
MEM_SLAB_MB="$(to_mb "$MEM_SLAB_KB")"

echo ""
echo "Guest RAM (idle, ~10s after boot):"
echo "  MemTotal     : ${MEM_TOTAL_MB} MB"
echo "  MemFree      : ${MEM_FREE_MB} MB"
echo "  MemAvailable : ${MEM_AVAIL_MB} MB"
echo "  Cached       : ${MEM_CACHED_MB} MB"
echo "  Slab         : ${MEM_SLAB_MB} MB"

MEM_AVAIL_FLOOR_KB=102400  # 100 MB (measured ~142MB idle)
if python3 -c "import sys; sys.exit(0 if int('$MEM_AVAIL_KB') > $MEM_AVAIL_FLOOR_KB else 1)"; then
    ok "MemAvailable=${MEM_AVAIL_MB}MB > 100MB"
else
    bad "MemAvailable=${MEM_AVAIL_MB}MB <= 100MB (dangerously low)"
fi

# ---------------------------------------------------------------------------
# 3. Guest process footprint
# ---------------------------------------------------------------------------
echo ""
echo "--- Guest process list (ps -w) ---"
PSOUT="$(_run "ps -w" 30)"
echo "$PSOUT"
PROC_COUNT="$(echo "$PSOUT" | grep -c '^\s*[0-9]' || true)"
echo "Total guest processes: $PROC_COUNT"

echo ""
echo "--- Guest top -bn1 (VSZ/CPU breakdown, also shows top RSS-equivalent consumers) ---"
TOPOUT="$(_run "top -bn1 | head -20" 30)"
echo "$TOPOUT"
ok "guest process table collected (count=$PROC_COUNT)"

# ---------------------------------------------------------------------------
# 4. Host qemu RSS
# ---------------------------------------------------------------------------
echo ""
echo "--- Host qemu RSS (pid=$QEMU_PID) ---"
HOST_RSS_LINE="$(ps -o rss=,vsz= -p "$QEMU_PID" 2>/dev/null || echo "0 0")"
HOST_RSS_KB="$(echo "$HOST_RSS_LINE" | awk '{print $1}')"
HOST_VSZ_KB="$(echo "$HOST_RSS_LINE" | awk '{print $2}')"
HOST_RSS_MB="$(python3 -c "print(round(int('${HOST_RSS_KB:-0}')/1024, 1))")"
HOST_VSZ_MB="$(python3 -c "print(round(int('${HOST_VSZ_KB:-0}')/1024, 1))")"
echo "  Host qemu RSS : ${HOST_RSS_MB} MB"
echo "  Host qemu VSZ : ${HOST_VSZ_MB} MB"

# Floor AND ceiling: a missing qemu pid makes `ps` yield "0 0", and 0 < ceiling
# would false-green a dead VM. Require RSS in a plausible live-VM band.
HOST_RSS_FLOOR_KB=51200   # 50 MB  (a live 256MB guest is never this small)
HOST_RSS_CEIL_KB=307200   # 300 MB (measured ~205MB settled; margin without slack)
if python3 -c "import sys; r=int('${HOST_RSS_KB:-0}'); sys.exit(0 if $HOST_RSS_FLOOR_KB < r < $HOST_RSS_CEIL_KB else 1)"; then
    ok "host_qemu_rss=${HOST_RSS_MB}MB in (50MB, 300MB) — live and within budget"
else
    bad "host_qemu_rss=${HOST_RSS_MB}MB outside (50MB,300MB): dead qemu or over budget"
fi

# ---------------------------------------------------------------------------
# 5. Guest idle loadavg (CAVEATED)
# ---------------------------------------------------------------------------
echo ""
echo "--- Guest idle loadavg (CAVEATED) ---"
LOADAVG="$(_run "cat /proc/loadavg" 30)"
echo "  /proc/loadavg : $LOADAVG"
echo ""
echo "CAVEAT: guest idle loadavg is NOT authoritative steady-state CPU."
echo "  Reason: sibling probe VMs run concurrently on the same host, and"
echo "  qemu:///session + slirp yields unrepresentative scheduling pressure."
echo "  Do NOT quote these numbers as real-workload CPU figures."
ok "guest idle loadavg collected (caveated)"

# ---------------------------------------------------------------------------
# 6. Boot run 2 (warm page cache)
# ---------------------------------------------------------------------------
echo ""
echo "--- Boot run 2 (warm host page cache) ---"
python3 "$SESSION_PY" down --rundir "$NETVM_RUNDIR" --purge
rm -rf "$NETVM_RUNDIR"
sleep 2

BOOT2_JSON="$(python3 "$SESSION_PY" up \
    --name "$NETVM_NAME" \
    --base "$NETVM_BASE" \
    --http-port "$NETVM_HTTP_PORT" \
    --rundir "$NETVM_RUNDIR" \
    --mem "$NETVM_MEM" \
    --no-provision)"

BOOT2_SECS="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["boot_secs"])' "$BOOT2_JSON")"
QEMU_PID2="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["pid"])' "$BOOT2_JSON")"
echo "Boot run 2: ${BOOT2_SECS}s"

HOST_RSS2_LINE="$(ps -o rss=,vsz= -p "$QEMU_PID2" 2>/dev/null || echo "0 0")"
HOST_RSS2_KB="$(echo "$HOST_RSS2_LINE" | awk '{print $1}')"
HOST_RSS2_MB="$(python3 -c "print(round(int('${HOST_RSS2_KB:-0}')/1024, 1))")"
echo "  Host qemu RSS run2: ${HOST_RSS2_MB} MB"

if python3 -c "import sys; sys.exit(0 if float('$BOOT2_SECS') < 60 else 1)"; then
    ok "boot_secs run2=${BOOT2_SECS} < 60"
else
    bad "boot_secs run2=${BOOT2_SECS} >= 60"
fi

# Same live-VM band as run 1, so a dead/over-budget qemu on the second boot fails
# too (rather than being merely reported in the summary).
if python3 -c "import sys; r=int('${HOST_RSS2_KB:-0}'); sys.exit(0 if $HOST_RSS_FLOOR_KB < r < $HOST_RSS_CEIL_KB else 1)"; then
    ok "host_qemu_rss run2=${HOST_RSS2_MB}MB in (50MB, 300MB) — live and within budget"
else
    bad "host_qemu_rss run2=${HOST_RSS2_MB}MB outside (50MB,300MB): dead qemu or over budget"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "======================================================================"
echo "s05 resource-budget summary"
echo "======================================================================"
echo "  Boot run 1 (cold page cache) : ${BOOT1_SECS}s"
echo "  Boot run 2 (warm page cache) : ${BOOT2_SECS}s"
echo "  Guest MemTotal               : ${MEM_TOTAL_MB} MB  (configured -m ${NETVM_MEM})"
echo "  Guest MemAvailable (idle)    : ${MEM_AVAIL_MB} MB"
echo "  Guest MemCached              : ${MEM_CACHED_MB} MB"
echo "  Guest Slab                   : ${MEM_SLAB_MB} MB"
echo "  Guest process count          : ${PROC_COUNT}"
echo "  Host qemu RSS run1 (settled) : ${HOST_RSS_MB} MB"
echo "  Host qemu VSZ run1           : ${HOST_VSZ_MB} MB"
echo "  Host qemu RSS run2           : ${HOST_RSS2_MB} MB"
echo "  Guest loadavg (CAVEATED)     : ${LOADAVG}"
echo ""
echo "  NOT MEASURED (refused):"
echo "    - Steady-state CPU under load: sibling VMs contaminate host scheduling"
echo "    - WAN throughput / latency:    slirp is not representative of real NICs"
echo "======================================================================"
echo "s05: pass=$pass fail=$fail"
echo "======================================================================"
[ "$fail" -eq 0 ]
