#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier5-loopback.
#
# Exercises spawn-tier5.sh --loopback over vsock_loopback (CID=1) using
# `wayland-info` as the inner client. wayland-info connects to the
# outer compositor through the waypipe-server → vsock → waypipe-client
# bridge, enumerates globals, and exits. If the data path is wired
# the journal will show one short-lived wl_client on the outer wayland
# socket; if it isn't, we never get a successful enumeration.
#
# Pairs with `tests/integration/permissions-gui/19-tier5-loopback-visible.md`
# which adds the visual chrome assertions using weston-terminal.
#
# PASS strings here MUST match the assert_output_contains in the bats
# @test phase7-tier5-loopback block.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SRC=/root/qdistro-src/qdistro
TIER5_DIR=/tmp/qdistro-tier5
# Stage the shared library too so a standalone replay is independent of files
# left in /tmp by earlier tiered-isolation cases.
if [ -d "$SRC/tier5-vm" ]; then
    rm -rf "$TIER5_DIR" 2>/dev/null || true
    mkdir -p "$TIER5_DIR"
    cp -r "$SRC/tier5-vm" "$TIER5_DIR/tier5-vm"
    cp -r "$SRC/lib" "$TIER5_DIR/lib"
    chmod -R a+rX "$TIER5_DIR"
    find "$TIER5_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
SPAWN="$TIER5_DIR/tier5-vm/spawn-tier5.sh"
[ -f "$SPAWN" ] || skip "tier5-vm source not unpacked at $SPAWN"
command -v waypipe >/dev/null 2>&1 || skip "waypipe not installed in this VM"
command -v wayland-info >/dev/null 2>&1 || skip "wayland-info (wayland-utils) not installed in this VM"

# vsock_loopback module — spawn-tier5.sh tries to modprobe; do it first
# so the failure mode is a clear SKIP instead of a spawn FAIL.
if ! lsmod | grep -q '^vsock_loopback'; then
    if ! modprobe vsock_loopback 2>/dev/null; then
        skip "vsock_loopback module not available in this kernel"
    fi
fi
pass "vsock_loopback module loaded"

ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER_SOCK="$RUNTIME_DIR/wayland-1"
if ! runuser -u admin -- test -S "$OUTER_SOCK"; then
    skip "outer admin compositor not up (wayland-1 socket missing)"
fi
pass "outer admin compositor up"

CURSOR=$(journalctl --since=now -n0 --show-cursor 2>/dev/null \
    | awk -F': ' '/-- cursor:/ {print $2}')

SPAWN_LOG=/tmp/s43-spawn.log
: >"$SPAWN_LOG"
PORT=7791

bash "$SPAWN" --loopback -p "$PORT" \
    -- wayland-info >"$SPAWN_LOG" 2>&1 &
SPAWN_PID=$!

LISTENER_OK=0
for _ in $(seq 1 40); do
    if grep -q "vsock listener ready cid=1 port=$PORT" "$SPAWN_LOG" 2>/dev/null; then
        LISTENER_OK=1; break
    fi
    sleep 0.25
done
if [ "$LISTENER_OK" = "0" ]; then
    cat "$SPAWN_LOG" >&2 || true
    fail "spawn-tier5.sh did not signal vsock listener ready within 10s"
fi

# wayland-info runs to completion in <2s once the bridge is up.
RC=255
for _ in $(seq 1 30); do
    if ! kill -0 "$SPAWN_PID" 2>/dev/null; then
        wait "$SPAWN_PID"; RC=$?
        break
    fi
    sleep 0.5
done
if kill -0 "$SPAWN_PID" 2>/dev/null; then
    kill -TERM "$SPAWN_PID" 2>/dev/null || true
    wait "$SPAWN_PID" 2>/dev/null || true
    fail "wayland-info never exited; bridge may be stalled"
fi

# wayland-info writes its enumeration to stdout — which spawn-tier5
# redirects into waypipe's server log. Look for the canonical
# `wl_compositor` line wayland-info emits.
SERVER_LOG=$(ls "$RUNTIME_DIR"/tier5-loopback-*-wayland-info-server.log 2>/dev/null | head -1)
if [ -n "$SERVER_LOG" ] && grep -q "interface: 'wl_compositor'" "$SERVER_LOG" 2>/dev/null; then
    pass "wayland-info enumerated wl_compositor through the vsock bridge"
elif grep -q "interface: 'wl_compositor'" "$SPAWN_LOG" 2>/dev/null; then
    pass "wayland-info enumerated wl_compositor through the vsock bridge"
else
    [ -n "$SERVER_LOG" ] && tail -40 "$SERVER_LOG" >&2 || true
    fail "wayland-info did not enumerate wl_compositor (bridge may not have completed handshake)"
fi

rm -f "$SPAWN_LOG" "$RUNTIME_DIR"/tier5-loopback-*-wayland-info-*.log 2>/dev/null || true

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-5-Linux waypipe-vsock loopback end-to-end"
    echo "[s43] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s43] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
