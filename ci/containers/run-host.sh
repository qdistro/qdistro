#!/usr/bin/env bash
# Private container entrypoint: use the existing logger and rows, without a
# second init_run/finish_run or any VM orchestration.
set -uo pipefail
[ -f /run/.containerenv ] || { echo 'ERROR: host rows require Podman' >&2; exit 2; }
QDISTRO_REPO=$(pwd)
export WORKSPACE=$QDISTRO_REPO
QCI_DIR=$QDISTRO_REPO/ci
QCI_LIB=$QCI_DIR/lib
export QCI_BIN_DIR=$QCI_DIR/bin
. "$QCI_LIB/bootstrap.sh"
RDIR=${QCI_HOST_RDIR:?}
. "$QCI_LIB/core.sh"
. "$QCI_LIB/run.sh"
. "$QCI_LIB/affected.sh"
. "$QCI_LIB/gates/host.sh"
{
    id
    printf 'container=%s\nQT_QPA_PLATFORM=%s\n' "${container:-unknown}" "$QT_QPA_PLATFORM"
    cat /proc/net/route
    python3 --version
    node --version
} > "$RDIR/host/container-runtime.txt"
host_container_rows
rc=$?
printf '%s\n' "$rc" > "$RDIR/host/container-complete"
exit "$rc"
