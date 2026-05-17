#!/bin/bash
# qdistro-tier4-publisher — runs inside the tier-4-guest VM.
#
# This file is the SOURCE-OF-TRUTH copy that lives in-tree under
# qdistro/tier4-vm-guest/. build-guest-image.sh copies it into the
# baked image at /usr/local/bin/. Shipping a free-standing copy here
# lets unit tests cover argv assembly + vsock-CID dispatch without
# booting a VM.
#
# Unlike tier-5 / tier-5b (which publish a single guest *app* via
# waypipe-server), tier-4-guest publishes the *nested qdwin's whole
# toplevel set* via qdwin-bystander --forward-all-toplevels wrapped by
# waypipe-server. The bystander binds inner qdwin's qdwin_shell_v1 on
# the in-guest wayland-0 socket and walks every inner xdg_toplevel;
# waypipe-server ferries the bystander's outbound Wayland connection
# across vsock to the host waypipe-client.
#
# Architecture: plan2/research/spice-retirement/00-overview.md §"nested
# qdwin in the guest". Phase: plan2/tasks/P10-tier4-guest-image-nested-
# qdwin.md §Phase C.
#
# Invoked by the in-guest systemd unit qdistro-tier4-publisher.service
# *or* by the host via qemu-guest-agent guest-exec:
#
#   qdistro-tier4-publisher.sh <vsock_port>
#
# vsock_port is allocated by the host-side spawn-tier4 (P11) via the
# same flock+CID range mechanism tier5b uses.

set -uo pipefail

# Inner qdwin socket. Defaults to the standard wayland-0 under the
# admin runtime dir; can be overridden for tests.
WAYLAND_SOCKET="${QDISTRO_TIER4_WAYLAND_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/1000}/wayland-0}"

# Bystander binary path. Image bake places it at /usr/bin/qdwin-bystander
# via the qdwin-guest package; tests override.
BYSTANDER_BIN="${QDISTRO_TIER4_BYSTANDER_BIN:-/usr/bin/qdwin-bystander}"

usage() {
    echo "usage: $0 <vsock_port>" >&2
    echo "  env: QDISTRO_TIER4_WAYLAND_SOCKET (default \$XDG_RUNTIME_DIR/wayland-0)" >&2
    echo "       QDISTRO_TIER4_BYSTANDER_BIN  (default /usr/bin/qdwin-bystander)" >&2
}

if [ $# -lt 1 ]; then
    usage
    exit 2
fi

PORT="$1"
shift

# Port sanity. 1..65535. Reject everything else loudly. (Same rule as
# tier5b-publisher; the lock-and-pick discipline lives in spawn-tier4,
# not here, so the publisher just validates the port it's handed.)
case "$PORT" in
    ''|*[!0-9]*)
        echo "[tier4-publisher] FAIL: port '$PORT' is not an integer" >&2
        exit 2
        ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "[tier4-publisher] FAIL: port '$PORT' out of range 1..65535" >&2
    exit 2
fi

# Wayland-socket sanity. Refuse path traversal / shell metacharacters
# even though we control the env: the systemd unit could in principle
# inherit a tampered XDG_RUNTIME_DIR via guest-agent guest-exec, and
# we'd rather fail-closed than glob-expand into something weird. The
# socket itself can be either an absolute path (we use it verbatim) or
# a basename (we resolve under $XDG_RUNTIME_DIR for the existence check
# but still pass the basename to --connect because that's what
# WAYLAND_DISPLAY expects).
case "$WAYLAND_SOCKET" in
    *\;*|*\|*|*\&*|*'$'*|*'`'*|*$'\n'*)
        echo "[tier4-publisher] FAIL: WAYLAND_SOCKET '$WAYLAND_SOCKET' contains forbidden chars" >&2
        exit 2
        ;;
esac

# Resolve absolute vs basename. --connect takes a basename to match
# wl_display_connect's lookup rules; if the operator handed us an
# absolute path, derive the basename and the runtime dir from it.
case "$WAYLAND_SOCKET" in
    /*)
        SOCK_DIR="$(dirname "$WAYLAND_SOCKET")"
        SOCK_NAME="$(basename "$WAYLAND_SOCKET")"
        ;;
    *)
        SOCK_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"
        SOCK_NAME="$WAYLAND_SOCKET"
        ;;
esac

LOG="${QDISTRO_TIER4_LOG:-/var/log/qdistro-tier4-publisher.log}"
if ! touch "$LOG" 2>/dev/null; then
    LOG="$(mktemp -t qdistro-tier4-publisher.XXXXXX.log)"
fi
exec >>"$LOG" 2>&1
echo "=== $(date -Iseconds): tier4-publisher port=$PORT sock_dir=$SOCK_DIR sock_name=$SOCK_NAME ==="

# Wait briefly for the vsock module on first boot (idempotent).
modprobe vhost_vsock 2>/dev/null || true
modprobe vsock 2>/dev/null || true
for _ in 1 2 3 4 5; do
    [ -e /dev/vsock ] && break
    sleep 0.5
done

# Wait for inner qdwin's wayland-0 to appear. The systemd unit
# qdistro-tier4-publisher.service has After=qdwin-guest-session.service
# but bring-up isn't synchronous on the Wayland-socket-ready event; a
# bounded poll plugs the race.
WAIT_BUDGET="${QDISTRO_TIER4_WAYLAND_WAIT:-30}"
for _ in $(seq 1 "$WAIT_BUDGET"); do
    [ -S "$SOCK_DIR/$SOCK_NAME" ] && break
    sleep 1
done
if [ ! -S "$SOCK_DIR/$SOCK_NAME" ]; then
    echo "[tier4-publisher] FAIL: inner Wayland socket $SOCK_DIR/$SOCK_NAME never appeared (waited ${WAIT_BUDGET}s)" >&2
    exit 3
fi

# qdwin-bystander needs XDG_RUNTIME_DIR to point at the socket dir
# (libwayland resolves wayland-0 under $XDG_RUNTIME_DIR).
export XDG_RUNTIME_DIR="$SOCK_DIR"

# Dry-run hook for unit tests: write the resolved argv to a tmpfile
# and exit 0 instead of execing waypipe (which isn't installed in the
# unit-test sandbox). Mirrors tier5b's QDISTRO_TIER5B_DRY_RUN.
if [ "${QDISTRO_TIER4_DRY_RUN:-0}" = "1" ]; then
    DRY_OUT="${QDISTRO_TIER4_DRY_OUT:-/tmp/qdistro-tier4-dry.log}"
    {
        echo "port=$PORT"
        echo "sock_dir=$SOCK_DIR"
        echo "sock_name=$SOCK_NAME"
        echo "bystander_bin=$BYSTANDER_BIN"
        echo "xdg_runtime_dir=$XDG_RUNTIME_DIR"
        echo "argv=waypipe --vsock -s 2:$PORT server -- $BYSTANDER_BIN --connect $SOCK_NAME --forward-all-toplevels"
    } >"$DRY_OUT"
    exit 0
fi

# Real path: connect out to host CID=2 on $PORT via waypipe-server,
# which exec's the bystander as its only wl_client. The bystander
# binds inner qdwin via $SOCK_NAME and emits FORWARD lines for every
# inner xdg_toplevel.
exec waypipe --vsock -s "2:$PORT" server -- \
    "$BYSTANDER_BIN" --connect "$SOCK_NAME" --forward-all-toplevels
