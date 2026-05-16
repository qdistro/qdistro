#!/bin/bash
# §Phase-7 tier-3 — bridge an app from a different-uid silo to the admin
# compositor via waypipe over AF_UNIX.
#
# Per doc/isolation-tiers.md "Tier 3 — different user (waypipe over UNIX)"
# and todo/qdwin-vm/tier3-spawn-design.md.
#
# Usage:
#   spawn-tier3.sh <silo> -- <app> [args...]
#
# Examples:
#   spawn-tier3.sh user1 -- weston-terminal
#   spawn-tier3.sh user1 -- firefox https://example.com
#
# The <silo> arg must be an existing local user that's a member of
# the qdistro-tier3 group (install-tier3-for-vm.sh defaults to
# user1+user2).
#
# Architecture:
#
#   [silo app, silo uid]               ← weston-terminal / firefox / etc
#         │  WAYLAND_DISPLAY=wayland-tier3-<silo>-$$
#         ▼
#   [waypipe server, silo uid]         ← runs as user1 via runuser
#         │  connects to bridge socket
#         ▼
#   [AF_UNIX bridge socket]            ← mode 0660 group qdistro-tier3
#         │  /run/user/$ADMIN_UID/qdistro-tier3-<silo>-<token>.sock
#         ▼
#   [waypipe client, admin uid]        ← wrapped by qdistro-secctx-exec
#         │  forwards into wayland-1
#         ▼
#   [admin compositor]
#
# Env knobs (mirror TIER2_* / TIER5_* where applicable):
#   TIER3_ADMIN_USER      Admin user (default "admin").
#   TIER3_OUTER_DISPLAY   WAYLAND_DISPLAY of the admin compositor
#                         (default "wayland-1").
#   TIER3_USE_SECCTX      Default 1 — wrap waypipe-client with
#                         qdistro-secctx-exec so the outer Wayland
#                         connection carries sandbox_engine + app_id
#                         + instance_id. Mirrors tier-4/tier-5.
#   TIER3_SECCTX_ENGINE   Default "qdistro.tier3".
#   TIER3_SECCTX_APPID    Default "qdistro.tier3.<silo>".
#   TIER3_SECCTX_INSTANCE Default <LAUNCH_TOKEN>.
#   TIER3_SECCTX          Legacy single-string secctx passed to waypipe
#                         --secctx (default "qdistro.tier3.<silo>"; set
#                         "" to disable). The load-bearing secctx is
#                         the triple set via qdistro-secctx-exec above.
#   TIER3_TITLE_PREFIX    Window title prefix. Default "[tier3:<silo>] ".
#   TIER3_NO_GPU          Block dmabuf via waypipe --no-gpu (default 1).
#   TIER3_DEBUG=1         Pass --debug to both waypipe halves.
#   TIER3_SOCKET_DIR      Where to place the bridge socket. Default
#                         /run/qdistro-tier3 (group-traversable;
#                         created by install-tier3-for-vm.sh). The
#                         silo uid needs +x on the dir to reach the
#                         socket path; admin's own /run/user/$UID is
#                         mode 0700 and excludes the silo entirely.
#   TIER3_GROUP           Group that gates silo→admin socket access.
#                         Default qdistro-tier3.
#   TIER3_SILO_RUNTIME    Per-launch XDG_RUNTIME_DIR for the silo half
#                         (default /tmp/qdistro-tier3-<token>). Created
#                         as the silo uid mode 0700, removed at exit.
#   TIER3_NO_REAP=1       Skip the at-startup orphan-socket reaper.
#   TIER3_REAP_AGE        Orphan-socket age threshold in seconds
#                         (default 86400 = 24h). Mirrors tier-2's
#                         orphan-dir reaper convention.
#
# Lifecycle:
#   1. Generate per-spawn LAUNCH_TOKEN (32 hex chars, /dev/urandom).
#      Emit `LAUNCH_TOKEN=<hex>` on stdout immediately (qdshell reads
#      this to seed cold-start placeholder correlation).
#   2. Validate <silo>: getent passwd + group membership in TIER3_GROUP.
#   3. Reap orphan bridge sockets > TIER3_REAP_AGE old whose wrapper
#      processes are gone (mirrors tier-2's at-startup orphan-dir reaper).
#   4. Start waypipe-client as admin (wrapped via qdistro-secctx-exec),
#      listening on the bridge socket path.
#   5. After waypipe creates the socket, chmod 0660 + chgrp TIER3_GROUP
#      so the silo uid (which is in TIER3_GROUP) can connect.
#   6. Create the silo's per-launch XDG_RUNTIME_DIR.
#   7. Start waypipe-server as the silo uid via runuser, connecting to
#      the bridge socket and exec'ing the silo cmd.
#   8. Wait for the silo cmd to exit; clean up both waypipe halves +
#      the bridge socket + the silo runtime dir.
#
# Exit code = silo app's exit code, or non-zero on bridge setup failure.

set -uo pipefail

usage() {
    sed -n '2,82p' "$0" | sed 's/^# \{0,1\}//'
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage; exit 0
fi
if [ $# -lt 1 ]; then
    usage >&2; exit 1
fi

SILO="${1:?usage: $0 <silo> -- <cmd> [args...]}"
shift
# Accept either `spawn-tier3.sh user1 -- weston-terminal` or
# `spawn-tier3.sh user1 weston-terminal`; the `--` is just a separator.
if [ "${1:-}" = "--" ]; then
    shift
fi
if [ $# -lt 1 ]; then
    echo "[tier3] FAIL: missing app to run" >&2
    usage >&2; exit 1
fi

# --- preflight --------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    echo "[tier3] FAIL: must run as root (uses runuser to drop to silo uid)" >&2
    echo "        try: sudo $0 $SILO -- $*" >&2
    exit 2
fi
if ! command -v waypipe >/dev/null 2>&1; then
    echo "[tier3] FAIL: waypipe not installed (zypper install waypipe)" >&2
    exit 2
fi

ADMIN_USER="${TIER3_ADMIN_USER:-admin}"
if ! id -u "$ADMIN_USER" >/dev/null 2>&1; then
    echo "[tier3] FAIL: admin user '$ADMIN_USER' does not exist" >&2
    exit 2
fi
ADMIN_UID=$(id -u "$ADMIN_USER")
ADMIN_RUNTIME="/run/user/$ADMIN_UID"
ADMIN_HOME="$(getent passwd "$ADMIN_USER" | cut -d: -f6)"
WAYLAND_DISPLAY="${TIER3_OUTER_DISPLAY:-${WAYLAND_DISPLAY:-wayland-1}}"

if [ ! -S "$ADMIN_RUNTIME/$WAYLAND_DISPLAY" ]; then
    echo "[tier3] FAIL: admin compositor socket $ADMIN_RUNTIME/$WAYLAND_DISPLAY not present" >&2
    exit 3
fi

# --- silo validation --------------------------------------------------
TIER3_GROUP="${TIER3_GROUP:-qdistro-tier3}"
if ! getent group "$TIER3_GROUP" >/dev/null; then
    echo "[tier3] FAIL: group '$TIER3_GROUP' does not exist (run install-tier3-for-vm.sh)" >&2
    exit 2
fi
if ! getent passwd "$SILO" >/dev/null; then
    echo "[tier3] FAIL: silo user '$SILO' does not exist" >&2
    exit 2
fi
if ! id -Gn "$SILO" | tr ' ' '\n' | grep -qx "$TIER3_GROUP"; then
    echo "[tier3] FAIL: silo user '$SILO' is not in group '$TIER3_GROUP'" >&2
    echo "        usermod -a -G $TIER3_GROUP $SILO" >&2
    exit 2
fi
SILO_UID=$(id -u "$SILO")
SILO_HOME=$(getent passwd "$SILO" | cut -d: -f6)
if [ "$SILO_UID" = "$ADMIN_UID" ]; then
    echo "[tier3] FAIL: silo '$SILO' has the same uid as admin '$ADMIN_USER'" >&2
    exit 2
fi

# --- LAUNCH_TOKEN + correlation ---------------------------------------
LAUNCH_TOKEN="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
if [ "${#LAUNCH_TOKEN}" -ne 32 ]; then
    echo "[tier3] FAIL: could not generate launch token from /dev/urandom" >&2
    exit 5
fi
APP_BASENAME=$(basename "$1")
# Emit correlation lines that qdshell's PodApps-style placeholder
# correlator reads. Always before any waypipe start so the
# token-watcher in qdshell catches it on stdout.
echo "LAUNCH_TOKEN=$LAUNCH_TOKEN"
echo "SILO=$SILO"
echo "APP_ID=$APP_BASENAME"

# --- secctx triple ----------------------------------------------------
USE_SECCTX="${TIER3_USE_SECCTX:-1}"
SECCTX_ENGINE="${TIER3_SECCTX_ENGINE:-qdistro.tier3}"
SECCTX_APPID="${TIER3_SECCTX_APPID:-qdistro.tier3.$SILO}"
SECCTX_INSTANCE="${TIER3_SECCTX_INSTANCE:-$LAUNCH_TOKEN}"
SECCTX="${TIER3_SECCTX-qdistro.tier3.$SILO}"
TITLE_PREFIX="${TIER3_TITLE_PREFIX:-[tier3:$SILO] }"
NO_GPU="${TIER3_NO_GPU:-1}"
DEBUG="${TIER3_DEBUG:-0}"

if [ "$USE_SECCTX" = "1" ] && ! command -v qdistro-secctx-exec >/dev/null 2>&1; then
    echo "[tier3] WARN: qdistro-secctx-exec not in PATH; placeholder correlation will not work" >&2
    USE_SECCTX=0
fi

# --- paths ------------------------------------------------------------
SOCKET_DIR="${TIER3_SOCKET_DIR:-/run/qdistro-tier3}"
BRIDGE_SOCK="$SOCKET_DIR/qdistro-tier3-$SILO-$LAUNCH_TOKEN.sock"
SILO_RUNTIME="${TIER3_SILO_RUNTIME:-/tmp/qdistro-tier3-$LAUNCH_TOKEN}"
INNER_DISPLAY="wayland-tier3-$SILO-$$"
CLIENT_LOG="$ADMIN_RUNTIME/tier3-${SILO}-${APP_BASENAME}-client.log"
SERVER_LOG="$ADMIN_RUNTIME/tier3-${SILO}-${APP_BASENAME}-server.log"
mkdir -p "$ADMIN_RUNTIME"
# Idempotent socket-dir setup. install-tier3-for-vm.sh creates this
# with the right perms persistently (+ tmpfiles.d), but be defensive
# in case the install was skipped or /run was wiped after reboot.
if [ ! -d "$SOCKET_DIR" ]; then
    install -d -o "$ADMIN_USER" -g "$TIER3_GROUP" -m 0710 "$SOCKET_DIR" || {
        echo "[tier3] FAIL: cannot create $SOCKET_DIR (group $TIER3_GROUP)" >&2
        exit 3
    }
fi
: >"$CLIENT_LOG" >"$SERVER_LOG"
chown "$ADMIN_USER" "$CLIENT_LOG" "$SERVER_LOG" 2>/dev/null || true

# --- orphan-socket reaper --------------------------------------------
# Bridge sockets at $SOCKET_DIR/qdistro-tier3-*-*.sock are orphaned
# when a prior spawn-tier3.sh wrapper died without running its EXIT
# trap (segfault, kill -9, host crash). Reap those older than
# TIER3_REAP_AGE seconds whose wrapper is no longer alive. Mirrors
# tier-2's at-startup orphan-dir reaper + tier-5's overlay reaper.
if [ "${TIER3_NO_REAP:-0}" != "1" ]; then
    REAP_AGE="${TIER3_REAP_AGE:-86400}"
    NOW=$(date +%s)
    for orphan in "$SOCKET_DIR"/qdistro-tier3-*-*.sock; do
        [ -S "$orphan" ] || continue
        [ "$orphan" = "$BRIDGE_SOCK" ] && continue
        otoken=$(basename "$orphan" .sock | awk -F- '{print $NF}')
        [ -z "$otoken" ] && continue
        # Skip if any spawn-tier3.sh wrapper still owns this token.
        if pgrep -af "spawn-tier3\.sh.*$otoken" >/dev/null 2>&1; then
            continue
        fi
        # Skip if newer than the reap threshold.
        mtime=$(stat -c %Y "$orphan" 2>/dev/null || echo "$NOW")
        if [ $((NOW - mtime)) -lt "$REAP_AGE" ]; then
            continue
        fi
        rm -f "$orphan" 2>/dev/null || true
        echo "[tier3] reaped orphan bridge socket $orphan" >&2
    done
fi

# --- cleanup trap -----------------------------------------------------
CLIENT_PID=
SERVER_PID=
cleanup() {
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
    [ -n "$CLIENT_PID" ] && kill "$CLIENT_PID" 2>/dev/null || true
    rm -f "$BRIDGE_SOCK" 2>/dev/null || true
    # Silo runtime dir owned by silo uid; rm as root.
    [ -d "$SILO_RUNTIME" ] && rm -rf "$SILO_RUNTIME" 2>/dev/null || true
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# --- 1. admin-side waypipe client (listens on bridge socket) ----------
CLIENT_OPTS=(-s "$BRIDGE_SOCK" -o)
[ "$NO_GPU" = "1" ] && CLIENT_OPTS+=(--no-gpu)
[ -n "$TITLE_PREFIX" ] && CLIENT_OPTS+=(--title-prefix "$TITLE_PREFIX")
[ "$DEBUG" = "1" ] && CLIENT_OPTS+=(--debug)
# IMPORTANT: only pass --secctx when the wrapper is *disabled*. When
# qdistro-secctx-exec wraps waypipe-client (USE_SECCTX=1, the default),
# waypipe-client runs *inside* a secctx-tagged Wayland session — and
# qdwin correctly hides wp_security_context_manager_v1 from already-
# tagged clients (preventing sub-sandbox escape). waypipe's --secctx
# tries to bind manager_v1 to plant its own tag → fails with
# "Compositor did not provide wp_security_context_manager_v1 global"
# and the bridge never comes up. The wrapper's secctx triple (engine
# + app_id + instance_id) is the load-bearing identity; waypipe's
# legacy single-string --secctx is redundant when wrapped.
if [ "$USE_SECCTX" != "1" ] && [ -n "$SECCTX" ]; then
    CLIENT_OPTS+=(--secctx "$SECCTX")
fi

SECCTX_WRAP=()
if [ "$USE_SECCTX" = "1" ]; then
    SECCTX_WRAP=(qdistro-secctx-exec
        --sandbox-engine "$SECCTX_ENGINE"
        --app-id         "$SECCTX_APPID"
        --instance-id    "$SECCTX_INSTANCE"
        --)
fi

runuser -u "$ADMIN_USER" -- env \
    WAYLAND_DISPLAY="$WAYLAND_DISPLAY" \
    XDG_RUNTIME_DIR="$ADMIN_RUNTIME" \
    HOME="$ADMIN_HOME" \
    "${SECCTX_WRAP[@]}" waypipe "${CLIENT_OPTS[@]}" client >"$CLIENT_LOG" 2>&1 &
CLIENT_PID=$!

# Wait for waypipe-client to create the socket, then chmod/chgrp so
# the silo uid (in TIER3_GROUP) can connect. Race window between
# socket creation and chmod is sub-ms; failure mode is silo-side
# ECONNREFUSED. Acceptable as a v1 (design doc §"open question 2").
SOCK_OK=0
for _ in $(seq 1 40); do
    if [ -S "$BRIDGE_SOCK" ]; then
        SOCK_OK=1; break
    fi
    sleep 0.25
done
if [ "$SOCK_OK" != "1" ]; then
    echo "[tier3] FAIL: waypipe-client did not create bridge socket within 10s" >&2
    cat "$CLIENT_LOG" >&2 || true
    exit 6
fi
# chgrp/chmod failures here are load-bearing (silo-side ECONNREFUSED
# if the perms aren't right). Don't swallow them silently; the log
# line below is the bats-asserted proof of correct setup.
if ! chgrp "$TIER3_GROUP" "$BRIDGE_SOCK" 2>/dev/null; then
    echo "[tier3] FAIL: chgrp $TIER3_GROUP on $BRIDGE_SOCK failed (admin not in group?)" >&2
    exit 6
fi
if ! chmod 0660 "$BRIDGE_SOCK" 2>/dev/null; then
    echo "[tier3] FAIL: chmod 0660 on $BRIDGE_SOCK failed" >&2
    exit 6
fi
echo "[tier3] bridge socket ready at $BRIDGE_SOCK ($TIER3_GROUP:0660)" >&2

# --- 2. silo-side runtime dir ----------------------------------------
# waypipe-server needs an XDG_RUNTIME_DIR the silo uid can write to
# for its inner Wayland socket. The silo's own /run/user/$SILO_UID
# may not exist (no graphical session); use a per-launch dir.
install -d -o "$SILO" -g "$SILO" -m 0700 "$SILO_RUNTIME"

# --- 3. silo-side waypipe server -------------------------------------
SERVER_OPTS=(-s "$BRIDGE_SOCK" -o)
[ "$NO_GPU" = "1" ] && SERVER_OPTS+=(--no-gpu)
[ "$DEBUG" = "1" ] && SERVER_OPTS+=(--debug)

echo "[tier3] $SILO (uid=$SILO_UID) → $BRIDGE_SOCK → $WAYLAND_DISPLAY" >&2
echo "[tier3] app: $*" >&2
echo "[tier3] logs: $CLIENT_LOG, $SERVER_LOG" >&2

runuser -u "$SILO" -- env \
    XDG_RUNTIME_DIR="$SILO_RUNTIME" \
    HOME="$SILO_HOME" \
    USER="$SILO" \
    LOGNAME="$SILO" \
    WAYLAND_DISPLAY="$INNER_DISPLAY" \
    waypipe "${SERVER_OPTS[@]}" server -- "$@" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

wait "$SERVER_PID" 2>/dev/null
EXIT=$?
echo "[tier3] silo app exited rc=$EXIT" >&2
exit "$EXIT"
