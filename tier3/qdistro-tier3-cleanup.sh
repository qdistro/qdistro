#!/bin/bash
# Tier-3 cleanup: remove orphan bridge sockets + per-launch silo
# runtime dirs left behind by crashed spawn-tier3.sh invocations.
# Idempotent.
#
# Usage:
#   qdistro-tier3-cleanup.sh              # reap orphans only
#   qdistro-tier3-cleanup.sh --all        # reap ALL bridge sockets,
#                                         # even ones owned by a live
#                                         # spawn wrapper (forces tear-
#                                         # down; use with care).
#   qdistro-tier3-cleanup.sh <token>      # reap one specific token
#
# Env knobs:
#   TIER3_ADMIN_USER  Admin user (default "admin").
#   TIER3_SOCKET_DIR  Bridge socket dir (default /run/qdistro-tier3 —
#                     matches spawn-tier3.sh's default since 2026-05-16;
#                     pre-fix builds used /run/user/$ADMIN_UID).
#   TIER3_REAP_AGE    Orphan age threshold in seconds (default 0 — the
#                     cleanup helper is more aggressive than the at-
#                     startup reaper baked into spawn-tier3.sh, which
#                     defaults to 86400s).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "[tier3-cleanup] FAIL: must run as root" >&2
    exit 2
fi

ADMIN_USER="${TIER3_ADMIN_USER:-admin}"
if ! id -u "$ADMIN_USER" >/dev/null 2>&1; then
    echo "[tier3-cleanup] FAIL: admin user '$ADMIN_USER' does not exist" >&2
    exit 2
fi
ADMIN_UID=$(id -u "$ADMIN_USER")
SOCKET_DIR="${TIER3_SOCKET_DIR:-/run/qdistro-tier3}"
REAP_AGE="${TIER3_REAP_AGE:-0}"
NOW=$(date +%s)
FORCE=0
TARGET_TOKEN=""

case "${1:-}" in
    --all) FORCE=1 ;;
    "")    ;;
    *)
        # Strict token validation to mirror spawn-tier3.sh's reaper.
        if ! [[ "$1" =~ ^[0-9a-f]{32}$ ]]; then
            echo "[tier3-cleanup] FAIL: token '$1' must be 32 lowercase hex chars (or use --all)" >&2
            exit 1
        fi
        TARGET_TOKEN="$1"
        ;;
esac

if [ ! -d "$SOCKET_DIR" ]; then
    echo "[tier3-cleanup] socket dir $SOCKET_DIR absent — nothing to reap"
    exit 0
fi

# Owner-alive check uses sidecar PID file (written by spawn-tier3 at
# socket-create time). Argv-greppable strings are forgeable by hostile
# silos via exec -a "...".
is_owner_alive() {
    local pid_file="$1"
    [ -f "$pid_file" ] || return 1   # no sidecar → presume orphan
    local opid
    opid=$(cat "$pid_file" 2>/dev/null || echo "")
    [[ "$opid" =~ ^[1-9][0-9]*$ ]] || return 1
    kill -0 "$opid" 2>/dev/null
}

REAPED=0
for sock in "$SOCKET_DIR"/qdistro-tier3-*-*.sock; do
    [ -e "$sock" ] || continue
    # Strict token extract: trailing 32 hex chars (silo names contain
    # no '-' per spawn-tier3.sh's validation, so $NF works, but a
    # regex match is robust against future filename schema changes).
    obase=${sock##*/}
    obase=${obase%.sock}
    otoken=${obase##*-}
    if ! [[ "$otoken" =~ ^[0-9a-f]{32}$ ]]; then
        continue
    fi
    if [ -n "$TARGET_TOKEN" ] && [ "$otoken" != "$TARGET_TOKEN" ]; then
        continue
    fi
    if [ "$FORCE" = "0" ] && is_owner_alive "$sock.pid"; then
        continue
    fi
    mtime=$(stat -c %Y "$sock" 2>/dev/null || echo "$NOW")
    if [ "$FORCE" = "0" ] && [ -z "$TARGET_TOKEN" ] && \
       [ $((NOW - mtime)) -lt "$REAP_AGE" ]; then
        continue
    fi
    if [ "$FORCE" = "1" ] && [ -f "$sock.pid" ]; then
        # Force-kill the owner by sidecar PID, not by argv grep.
        opid=$(cat "$sock.pid" 2>/dev/null || echo "")
        if [[ "$opid" =~ ^[1-9][0-9]*$ ]]; then
            kill "$opid" 2>/dev/null || true
        fi
    fi
    rm -f "$sock" "$sock.pid" 2>/dev/null || true
    rm -rf "$SOCKET_DIR/runtime-$otoken" 2>/dev/null || true
    # Legacy /tmp dir from pre-2026-05-16 builds.
    rm -rf "/tmp/qdistro-tier3-$otoken" 2>/dev/null || true
    REAPED=$((REAPED + 1))
    echo "[tier3-cleanup] reaped $sock"
done

# Sweep leftover silo runtime dirs whose matching socket is already
# gone (a crash between socket-cleanup and runtime-dir-cleanup can
# leave the runtime subdir behind).
for rt in "$SOCKET_DIR"/runtime-* /tmp/qdistro-tier3-*; do
    [ -d "$rt" ] || continue
    rt_base=${rt##*/}
    case "$rt_base" in
        runtime-*) otoken=${rt_base#runtime-} ;;
        qdistro-tier3-*) otoken=${rt_base#qdistro-tier3-} ;;
        *) continue ;;
    esac
    if ! [[ "$otoken" =~ ^[0-9a-f]{32}$ ]]; then
        continue
    fi
    if [ -n "$TARGET_TOKEN" ] && [ "$otoken" != "$TARGET_TOKEN" ]; then
        continue
    fi
    # Token-uniqueness invariant: at most one socket per $otoken. The
    # glob runs unquoted to expand; if multiple matches existed
    # (impossible by design), only the first would be checked. Same
    # behaviour as the round-1 reaper, made explicit here.
    if [ "$FORCE" = "0" ]; then
        owner_check_path=""
        for cand in "$SOCKET_DIR"/qdistro-tier3-*-"$otoken".sock.pid; do
            [ -f "$cand" ] && { owner_check_path="$cand"; break; }
        done
        if [ -n "$owner_check_path" ] && is_owner_alive "$owner_check_path"; then
            continue
        fi
    fi
    rm -rf "$rt" 2>/dev/null || true
done

echo "[tier3-cleanup] done (reaped $REAPED bridge sockets)"
