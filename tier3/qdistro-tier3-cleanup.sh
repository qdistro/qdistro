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
#   TIER3_SOCKET_DIR  Bridge socket dir (default /run/user/$ADMIN_UID).
#   TIER3_REAP_AGE    Orphan age threshold in seconds (default 0 — the
#                     cleanup helper is more aggressive than the at-
#                     startup reaper baked into spawn-tier3.sh, which
#                     defaults to 86400s).
set -uo pipefail

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
SOCKET_DIR="${TIER3_SOCKET_DIR:-/run/user/$ADMIN_UID}"
REAP_AGE="${TIER3_REAP_AGE:-0}"
NOW=$(date +%s)
FORCE=0
TARGET_TOKEN=""

case "${1:-}" in
    --all) FORCE=1 ;;
    "")    ;;
    *)     TARGET_TOKEN="$1" ;;
esac

if [ ! -d "$SOCKET_DIR" ]; then
    echo "[tier3-cleanup] socket dir $SOCKET_DIR absent — nothing to reap"
    exit 0
fi

REAPED=0
for sock in "$SOCKET_DIR"/qdistro-tier3-*-*.sock; do
    [ -e "$sock" ] || continue
    base=$(basename "$sock" .sock)
    otoken=$(echo "$base" | awk -F- '{print $NF}')
    [ -z "$otoken" ] && continue
    if [ -n "$TARGET_TOKEN" ] && [ "$otoken" != "$TARGET_TOKEN" ]; then
        continue
    fi
    if [ "$FORCE" = "0" ] && pgrep -af "spawn-tier3\.sh.*$otoken" >/dev/null 2>&1; then
        continue
    fi
    mtime=$(stat -c %Y "$sock" 2>/dev/null || echo "$NOW")
    if [ "$FORCE" = "0" ] && [ -z "$TARGET_TOKEN" ] && \
       [ $((NOW - mtime)) -lt "$REAP_AGE" ]; then
        continue
    fi
    if [ "$FORCE" = "1" ]; then
        pkill -f "spawn-tier3\.sh.*$otoken" 2>/dev/null || true
    fi
    rm -f "$sock" 2>/dev/null || true
    rm -rf "/tmp/qdistro-tier3-$otoken" 2>/dev/null || true
    REAPED=$((REAPED + 1))
    echo "[tier3-cleanup] reaped $sock"
done

# Sweep leftover silo runtime dirs whose matching socket is already
# gone (the socket-side reap above usually catches both, but a crash
# between socket-cleanup and runtime-dir-cleanup can leave just the
# /tmp/qdistro-tier3-<token> behind).
for rt in /tmp/qdistro-tier3-*; do
    [ -d "$rt" ] || continue
    otoken=$(basename "$rt" | sed 's/^qdistro-tier3-//')
    [ -z "$otoken" ] && continue
    if [ -n "$TARGET_TOKEN" ] && [ "$otoken" != "$TARGET_TOKEN" ]; then
        continue
    fi
    if [ "$FORCE" = "0" ] && pgrep -af "spawn-tier3\.sh.*$otoken" >/dev/null 2>&1; then
        continue
    fi
    rm -rf "$rt" 2>/dev/null || true
done

echo "[tier3-cleanup] done (reaped $REAPED bridge sockets)"
