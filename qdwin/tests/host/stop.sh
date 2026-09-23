#!/bin/bash
# Tear down the test compositor + clients for a scenario.
#
# Usage: stop.sh <test-id> [--keep-logs]
#
# Default: kill all spawned processes AND remove the per-test dir.
# With --keep-logs: kill processes, keep $TEST_ROOT/<id>/ for inspection.

set -eo pipefail
. "$(dirname "$0")/lib.sh"

TEST_ID=${1:?usage: stop.sh <test-id> [--keep-logs]}
KEEP_LOGS=0
[ "${2:-}" = "--keep-logs" ] && KEEP_LOGS=1

DIR=$(ht_dir "$TEST_ID")
[ -d "$DIR" ] || { echo "[stop.sh] no state at $DIR — already stopped?"; exit 0; }

# Kill in reverse-spawn order.
for role in qdshell terminal weston; do
    pid=$(ht_pid_load "$TEST_ID" "$role")
    [ -n "$pid" ] && ht_kill "$pid"
done
sleep 0.4
# Force-kill any survivors.
for role in qdshell terminal weston; do
    pid=$(ht_pid_load "$TEST_ID" "$role")
    [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true
done

if [ "$KEEP_LOGS" = 0 ]; then
    rm -rf "$DIR"
else
    # Just clear the runtime + command FIFO — logs stay.
    rm -rf "$DIR/runtime" "$(ht_ctrl "$TEST_ID")" "$DIR/pids"
fi
