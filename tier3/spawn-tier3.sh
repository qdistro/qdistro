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
#   TIER3_SILO_RUNTIME    Per-launch XDG_RUNTIME_DIR for the silo half
#                         (default $TIER3_SOCKET_DIR/runtime-<token>).
#                         Created as the silo uid mode 0700, removed at
#                         exit. Refused via pkexec for security.
#                         (Pre-2026-05-16 builds defaulted to
#                         /tmp/qdistro-tier3-<token>; legacy paths are
#                         still reaped by the orphan sweeper.)
#
#   Note: the group name is NOT a runtime knob. It's hard-coded to
#   "qdistro-tier3" inside spawn-tier3.sh because the bats wrappers
#   grep the literal string in spawn-tier3's log output. The install
#   script's own TIER3_GROUP knob is independent (controls what
#   install-tier3-for-vm.sh creates) — override both in lockstep if
#   you really need a different name.
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

# Promoted to errexit on 2026-05-16: silent mid-flight failures are
# load-bearing for the bridge (a failed install -d / chown / runuser
# is not recoverable, and the bats wrappers grep specific log lines
# emitted *after* successful setup). The few `|| true` lines below
# are the legitimate failure-tolerant spots (orphan reaper, log
# chowns, cleanup trap).
set -euo pipefail

usage() {
    cat <<'EOF'
spawn-tier3.sh <silo> -- <cmd> [args...]

Bridge an app from a different-uid silo to the admin compositor via
waypipe over AF_UNIX. See header comment block for the full env-knob
reference. Must run as root.
EOF
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

# Silo name validation. Rejecting `-` here is load-bearing for the
# orphan-reaper's token parser, which uses the trailing-32-hex regex
# to extract the LAUNCH_TOKEN from the socket filename. The reaper
# also stores the wrapper PID in a sidecar file (see SOCK_PID_FILE
# below), so the live-owner check no longer relies on argv-greppable
# strings — but rejecting non-`[A-Za-z_][A-Za-z0-9_]*` silo names
# stays as defense in depth (matches POSIX portable-username, sans
# `-`).
if ! [[ "$SILO" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "[tier3] FAIL: silo name '$SILO' must match [A-Za-z_][A-Za-z0-9_]* (no '-' or other special chars)" >&2
    exit 1
fi

# --- preflight --------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    # Don't mirror attacker-controlled args back to stderr (terminal
    # escape injection risk on TUI viewers). Print a fixed hint.
    echo "[tier3] FAIL: must run as root (uses runuser to drop to silo uid)" >&2
    echo "        try: sudo $(basename "$0") <silo> -- <cmd> [args...]" >&2
    exit 2
fi

# When invoked via pkexec (active admin session), refuse env knobs
# that could weaken the security boundary. The polkit policy gates
# *which* binary admin-session processes can run as root; this gates
# the env those processes can pass.
if [ -n "${PKEXEC_UID:-}" ]; then
    # Block any env knob that affects path, identity, or sandbox
    # hardening. TIER3_NO_REAP / TIER3_REAP_AGE included so a captive
    # admin-session attacker can't leak old sockets across boots.
    # Cosmetic knobs (TITLE_PREFIX, NO_GPU, DEBUG) are left allowed.
    for danger in \
        TIER3_SOCKET_DIR TIER3_SILO_RUNTIME TIER3_USE_SECCTX \
        TIER3_GROUP TIER3_ADMIN_USER \
        TIER3_NO_REAP TIER3_REAP_AGE \
        TIER3_SECCTX TIER3_SECCTX_ENGINE TIER3_SECCTX_APPID TIER3_SECCTX_INSTANCE \
        TIER3_OUTER_DISPLAY; do
        if [ -n "${!danger:-}" ]; then
            echo "[tier3] FAIL: env knob '$danger' not allowed via pkexec (PKEXEC_UID=$PKEXEC_UID set)" >&2
            exit 2
        fi
    done
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
# Group name is intentionally NOT configurable. The bats drivers grep
# for the literal "qdistro-tier3" in spawn-tier3's "bridge socket
# ready" log line; making the group name an env knob silently breaks
# all eight tests with no compile-time signal. The install script's
# TIER3_GROUP knob is independent (it controls what install-tier3-
# for-vm.sh creates) — if you really need a different group name in
# production, override both sides in lockstep.
TIER3_GROUP="qdistro-tier3"
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
# shellcheck source=../lib/spawn-common.sh
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s\n' "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
SPAWN_COMMON="$SCRIPT_DIR/../lib/spawn-common.sh"
if [ ! -r "$SPAWN_COMMON" ] && [ -r /usr/local/lib/qdistro/spawn-common.sh ]; then
    SPAWN_COMMON=/usr/local/lib/qdistro/spawn-common.sh
elif [ ! -r "$SPAWN_COMMON" ] && [ -r /usr/lib/qdistro/spawn-common.sh ]; then
    SPAWN_COMMON=/usr/lib/qdistro/spawn-common.sh
fi
if [ ! -r "$SPAWN_COMMON" ]; then
    echo "[tier3] FAIL: spawn-common.sh not found (looked near $SCRIPT_DIR, /usr/local/lib/qdistro, and /usr/lib/qdistro)" >&2
    exit 5
fi
. "$SPAWN_COMMON"
LAUNCH_TOKEN="$(gen_launch_token "[tier3] FAIL")"
APP_BASENAME=$(basename "$1")
# Emit correlation lines that qdshell's PodApps-style placeholder
# correlator reads. Always before any waypipe start so the
# token-watcher in qdshell catches it on stdout. Prefixed `[tier3]`
# so mixed-stream parsers can filter cleanly (tier-5 historically
# emitted bare KEY=VAL lines; we don't repeat that mistake).
echo "[tier3] LAUNCH_TOKEN=$LAUNCH_TOKEN"
echo "[tier3] SILO=$SILO"
echo "[tier3] APP_ID=$APP_BASENAME"

# --- secctx triple ----------------------------------------------------
USE_SECCTX="${TIER3_USE_SECCTX:-1}"
SECCTX_ENGINE="${TIER3_SECCTX_ENGINE:-qdistro.tier3}"
SECCTX_APPID="${TIER3_SECCTX_APPID:-qdistro.tier3.$SILO}"
SECCTX_INSTANCE="${TIER3_SECCTX_INSTANCE:-$LAUNCH_TOKEN}"
SECCTX="${TIER3_SECCTX-qdistro.tier3.$SILO}"
TITLE_PREFIX="${TIER3_TITLE_PREFIX:-[tier3:$SILO] }"
NO_GPU="${TIER3_NO_GPU:-1}"
DEBUG="${TIER3_DEBUG:-0}"

# Previously this silently flipped USE_SECCTX=0 with a WARN. That's a
# silent security-boundary downgrade: the tier-3 toplevel arrives at
# qdwin without a wp_security_context_v1 tag, so qdshell's Tier3Apps
# never classifies it and the clipboard gate sees it as plain admin.
# Hard-fail unless the operator explicitly opts out via TIER3_USE_SECCTX=0.
if [ "$USE_SECCTX" = "1" ] && ! command -v qdistro-secctx-exec >/dev/null 2>&1; then
    echo "[tier3] FAIL: qdistro-secctx-exec not in PATH" >&2
    echo "        This is required to plant the wp_security_context_v1 triple" >&2
    echo "        (engine + app_id + instance_id). Without it the silo toplevel" >&2
    echo "        reaches qdwin un-tagged and the clipboard gate silently treats" >&2
    echo "        it as admin. To bypass (NOT recommended; security downgrade):" >&2
    echo "          TIER3_USE_SECCTX=0 $(basename "$0") $SILO -- <cmd>" >&2
    exit 2
fi

# --- paths ------------------------------------------------------------
SOCKET_DIR="${TIER3_SOCKET_DIR:-/run/qdistro-tier3}"
BRIDGE_SOCK="$SOCKET_DIR/qdistro-tier3-$SILO-$LAUNCH_TOKEN.sock"
SOCK_PID_FILE="$BRIDGE_SOCK.pid"
# Silo runtime dir lives inside $SOCKET_DIR (mode 0710 group-traverse-
# only). The per-launch subdir is owned by the silo uid mode 0700, so
# other silos in the group can traverse the parent but can't enumerate
# (no `r` on parent) nor enter (no `r,x` on siblings' runtime dir).
# Previously /tmp/qdistro-tier3-<token> exposed directory names via
# /tmp's world-readable listing — minor side-channel resolved.
SILO_RUNTIME="${TIER3_SILO_RUNTIME:-$SOCKET_DIR/runtime-$LAUNCH_TOKEN}"
INNER_DISPLAY="wayland-tier3-$SILO-$$"
CLIENT_LOG="$ADMIN_RUNTIME/tier3-${SILO}-${APP_BASENAME}-client.log"
SERVER_LOG="$ADMIN_RUNTIME/tier3-${SILO}-${APP_BASENAME}-server.log"
mkdir -p "$ADMIN_RUNTIME"
# Idempotent socket-dir setup. install-tier3-for-vm.sh creates this
# with the right perms persistently (+ tmpfiles.d), but be defensive
# in case the install was skipped or /run was wiped after reboot.
# Verify perms even when the dir already exists — drift detection.
if [ ! -d "$SOCKET_DIR" ]; then
    install -d -o "$ADMIN_USER" -g "$TIER3_GROUP" -m 0710 "$SOCKET_DIR" || {
        echo "[tier3] FAIL: cannot create $SOCKET_DIR (group $TIER3_GROUP)" >&2
        exit 3
    }
else
    # Reset perms in case tmpfiles.d didn't run or a prior boot left
    # them wide-open. Hard-fail if we can't normalize.
    chown "$ADMIN_USER:$TIER3_GROUP" "$SOCKET_DIR"
    chmod 0710 "$SOCKET_DIR"
fi
# Rotate previous-run logs so a failed previous spawn's evidence
# survives one cycle. Truncating-on-spawn was hiding causes of
# back-to-back failures.
for log in "$CLIENT_LOG" "$SERVER_LOG"; do
    [ -f "$log" ] && mv -f "$log" "$log.prev" 2>/dev/null || true
done
: >"$CLIENT_LOG" >"$SERVER_LOG"
chown "$ADMIN_USER" "$CLIENT_LOG" "$SERVER_LOG" 2>/dev/null || true

# --- orphan-socket reaper --------------------------------------------
# Sockets at $SOCKET_DIR/qdistro-tier3-*-*.sock are orphaned when a
# prior spawn-tier3.sh wrapper died without running its EXIT trap
# (segfault, kill -9, host crash). Reap entries:
#   1. that aren't OUR new socket
#   2. whose owning PID is gone (sidecar .pid file reads as dead, or
#      no sidecar at all → assume orphaned by an older spawn version)
#   3. older than $REAP_AGE seconds
#
# Token extraction uses the strict ${file%.sock} → trailing-32-hex
# pattern: silo names contain no `-` (validated above), so the token
# is everything after the final `-`. The PID check reads from a
# sidecar file we write at socket-create time (no more argv-greppable
# substring matching — a hostile silo can't forge `pgrep -af` results
# by setting argv to fake an owner anymore).
if [ "${TIER3_NO_REAP:-0}" != "1" ]; then
    REAP_AGE="${TIER3_REAP_AGE:-86400}"
    NOW=$(date +%s)
    for orphan in "$SOCKET_DIR"/qdistro-tier3-*-*.sock; do
        [ -S "$orphan" ] || continue
        [ "$orphan" = "$BRIDGE_SOCK" ] && continue
        # Strict token extract: <socket>.sock → trailing 32 hex.
        obase=${orphan##*/}
        obase=${obase%.sock}
        otoken=${obase##*-}
        if ! [[ "$otoken" =~ ^[0-9a-f]{32}$ ]]; then
            continue
        fi
        # Owner check via sidecar PID file (not argv greppable).
        opid_file="$orphan.pid"
        if [ -f "$opid_file" ]; then
            opid=$(cat "$opid_file" 2>/dev/null || echo "")
            if [[ "$opid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$opid" 2>/dev/null; then
                continue   # owner still alive
            fi
        fi
        # Age threshold.
        mtime=$(stat -c %Y "$orphan" 2>/dev/null || echo "$NOW")
        if [ $((NOW - mtime)) -lt "$REAP_AGE" ]; then
            continue
        fi
        rm -f "$orphan" "$opid_file" 2>/dev/null || true
        # Reap the matching runtime subdir if it lives in $SOCKET_DIR.
        rm -rf "$SOCKET_DIR/runtime-$otoken" 2>/dev/null || true
        # Legacy /tmp/qdistro-tier3-<token> from pre-fix builds.
        rm -rf "/tmp/qdistro-tier3-$otoken" 2>/dev/null || true
        echo "[tier3] reaped orphan bridge socket $orphan" >&2
    done
fi

# --- cleanup trap -----------------------------------------------------
CLIENT_PID=
SERVER_PID=
cleanup() {
    # cleanup runs under `set -e` — silence individual failures so a
    # half-set-up state still tries every step.
    set +e
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
    [ -n "$CLIENT_PID" ] && kill "$CLIENT_PID" 2>/dev/null
    rm -f "$BRIDGE_SOCK" "$SOCK_PID_FILE" 2>/dev/null
    # Silo runtime dir owned by silo uid; rm as root.
    [ -d "$SILO_RUNTIME" ] && rm -rf "$SILO_RUNTIME" 2>/dev/null
    return 0
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

# H1 fix (2026-05-16): set a restrictive umask before backgrounding
# waypipe so the listen socket is BORN mode 0660, not 0755. Previously
# the chgrp/chmod ran AFTER waypipe accepted the first connection —
# during the race window /run/qdistro-tier3 (mode 0710 group-traverse)
# let any silo in the group connect()  by full path while the socket
# was still admin:admin 0755. Cross-silo bridge-hijack window, closed.
#
# umask 0117 → world-clear, group-keep (file: 0660, dir: 0660-ish).
# waypipe might explicitly chmod its own socket, but our chgrp below
# normalizes the group regardless.
runuser -u "$ADMIN_USER" -- env \
    WAYLAND_DISPLAY="$WAYLAND_DISPLAY" \
    XDG_RUNTIME_DIR="$ADMIN_RUNTIME" \
    HOME="$ADMIN_HOME" \
    bash -c 'umask 0117; exec "$@"' bash \
    "${SECCTX_WRAP[@]}" waypipe "${CLIENT_OPTS[@]}" client >"$CLIENT_LOG" 2>&1 &
CLIENT_PID=$!

# Wait for waypipe-client to create the socket (it'll be born 0660
# admin:admin per umask above; we still need chgrp to flip the group
# from admin:admin to admin:qdistro-tier3 so silo members can connect).
SOCK_OK=0
for _ in $(seq 1 40); do
    if [ -S "$BRIDGE_SOCK" ]; then
        SOCK_OK=1; break
    fi
    if ! kill -0 "$CLIENT_PID" 2>/dev/null; then
        # waypipe died before creating the socket; bail with its log.
        break
    fi
    sleep 0.25
done
if [ "$SOCK_OK" != "1" ]; then
    echo "[tier3] FAIL: waypipe-client did not create bridge socket within 10s" >&2
    cat "$CLIENT_LOG" >&2 || true
    exit 6
fi

# Defense-in-depth: even with the umask the socket exists 0660
# admin:admin until our chgrp+chmod. No silo can `connect()` because
# they're not in `admin` group. Now flip group to qdistro-tier3
# (mode is already 0660; chmod is a no-op confirm).
chgrp "$TIER3_GROUP" "$BRIDGE_SOCK"
chmod 0660 "$BRIDGE_SOCK"

# Write the sidecar PID file used by future orphan reapers. Make it
# group-readable so any silo can verify a sibling spawn is alive
# (informational; the security gate is the socket perms above).
echo "$$" > "$SOCK_PID_FILE"
chgrp "$TIER3_GROUP" "$SOCK_PID_FILE"
chmod 0640 "$SOCK_PID_FILE"

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
