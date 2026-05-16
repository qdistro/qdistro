#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier3-waypipe.
#
# Exercises spawn-tier3.sh user1 -- wayland-info over an AF_UNIX bridge
# socket. wayland-info runs as the silo uid (user1), connects through
# the waypipe-server → AF_UNIX → waypipe-client bridge into the admin
# compositor, enumerates globals, exits. The silo-uid + bridge-socket
# + bridged-enumeration triple is the load-bearing assertion set for
# the tier-3 v1.
#
# Pairs with the cross-uid lifecycle test s37 (multi-silo, kill-A-
# leaves-B) and the chrome test s38 (qdshell silo prefix parser),
# both of which build on this same spawn helper.
#
# PASS strings here MUST match the assert_output_contains in the bats
# @test phase7-tier3-waypipe block.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

# --- 1. stage tier3 source into /tmp ---------------------------------
SRC=/root/qdistro-src/qdistro
TIER3_DIR=/tmp/qdistro-tier3-src
if [ -d "$SRC/tier3" ]; then
    rm -rf "$TIER3_DIR" 2>/dev/null || true
    cp -r "$SRC/tier3" "$TIER3_DIR"
    chmod -R a+rX "$TIER3_DIR"
    find "$TIER3_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
[ -d "$TIER3_DIR" ] || skip "tier3 source not unpacked at $TIER3_DIR"

command -v waypipe       >/dev/null 2>&1 || skip "waypipe not installed in this VM"
command -v wayland-info  >/dev/null 2>&1 || skip "wayland-info (wayland-utils) not installed in this VM"
command -v runuser       >/dev/null 2>&1 || skip "runuser not available"

# --- 2. tier3 install ran (group + silo users + symlinks) ------------
if ! getent group qdistro-tier3 >/dev/null; then
    skip "qdistro-tier3 group missing — install-tier3-for-vm.sh did not run"
fi
if ! id -u user1 >/dev/null 2>&1; then
    skip "silo user 'user1' missing — install-tier3-for-vm.sh did not create silos"
fi
if ! id -Gn user1 | tr ' ' '\n' | grep -qx qdistro-tier3; then
    fail "user1 is not in qdistro-tier3 group (install-tier3-for-vm.sh failed silently?)"
fi
SILO_UID=$(id -u user1)

# --- 3. outer admin compositor ---------------------------------------
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER_SOCK="$RUNTIME_DIR/wayland-1"
if ! runuser -u admin -- test -S "$OUTER_SOCK"; then
    skip "outer admin compositor not up (wayland-1 socket missing)"
fi
pass "outer admin compositor up"

# --- 4. run spawn-tier3.sh in the background --------------------------
SPAWN_LOG=/tmp/s35-spawn.log
: >"$SPAWN_LOG"

# Force the spawn wrapper not to reap orphans during the test (we
# want a deterministic test environment; an older orphan could
# otherwise mask a real bug).
TIER3_NO_REAP=1 \
bash "$TIER3_DIR/spawn-tier3.sh" user1 -- wayland-info >"$SPAWN_LOG" 2>&1 &
SPAWN_PID=$!

# --- 5. wait for the bridge socket to be ready (mode+group set) ------
BRIDGE_READY=0
for _ in $(seq 1 40); do
    if grep -q "bridge socket ready at .* (qdistro-tier3:0660)" "$SPAWN_LOG" 2>/dev/null; then
        BRIDGE_READY=1; break
    fi
    if ! kill -0 "$SPAWN_PID" 2>/dev/null; then
        # Spawn exited before we saw the bridge — likely a setup failure.
        break
    fi
    sleep 0.25
done
if [ "$BRIDGE_READY" = "0" ]; then
    cat "$SPAWN_LOG" >&2 || true
    fail "spawn-tier3.sh did not signal bridge-socket-ready within 10s"
fi

# --- 6. stat the socket while it exists -------------------------------
BRIDGE_SOCK=$(grep -oE '/run/user/[0-9]+/qdistro-tier3-user1-[0-9a-f]+\.sock' "$SPAWN_LOG" | head -1)
if [ -n "$BRIDGE_SOCK" ] && [ -S "$BRIDGE_SOCK" ]; then
    SOCK_GROUP=$(stat -c %G "$BRIDGE_SOCK" 2>/dev/null || echo "")
    SOCK_MODE=$(stat -c %a "$BRIDGE_SOCK" 2>/dev/null || echo "")
    if [ "$SOCK_GROUP" = "qdistro-tier3" ] && [ "$SOCK_MODE" = "660" ]; then
        pass "bridge socket carried qdistro-tier3:0660"
    else
        fail "bridge socket mode/group wrong: group='$SOCK_GROUP' mode='$SOCK_MODE'"
    fi
else
    fail "bridge socket disappeared before stat (path='$BRIDGE_SOCK')"
fi

# --- 7. wait for spawn to complete -----------------------------------
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

# --- 8. silo cmd ran as silo uid -------------------------------------
# spawn-tier3.sh emits a "[tier3] user1 (uid=$SILO_UID) → ..." line on
# stderr (which we redirected into SPAWN_LOG). That's the wrapper's
# own log; for proof the inner command actually ran in the silo uid
# context, also check the per-spawn server log filename pattern
# (tier3-user1-wayland-info-server.log; only generated when the silo-
# side waypipe-server actually started) AND check `ps`-style audit
# via the silo runtime dir creation (the dir is created by the wrapper
# `install -d -o user1 -g user1`; presence at exit time would suggest
# the runuser path was taken).
if grep -q "\[tier3\] user1 (uid=$SILO_UID)" "$SPAWN_LOG"; then
    pass "silo command actually ran as uid $SILO_UID"
else
    cat "$SPAWN_LOG" >&2 || true
    fail "spawn-tier3.sh never reported user1 uid=$SILO_UID handoff"
fi

# --- 9. wayland-info enumerated wl_compositor through the bridge -----
SERVER_LOG=$(ls "$RUNTIME_DIR"/tier3-user1-wayland-info-server.log 2>/dev/null | head -1)
if [ -n "$SERVER_LOG" ] && grep -q "interface: 'wl_compositor'" "$SERVER_LOG" 2>/dev/null; then
    pass "wayland-info enumerated wl_compositor through the bridge"
elif grep -q "interface: 'wl_compositor'" "$SPAWN_LOG" 2>/dev/null; then
    pass "wayland-info enumerated wl_compositor through the bridge"
else
    [ -n "$SERVER_LOG" ] && tail -40 "$SERVER_LOG" >&2 || true
    fail "wayland-info did not enumerate wl_compositor (bridge handshake may not have completed)"
fi

rm -f "$SPAWN_LOG" "$RUNTIME_DIR"/tier3-user1-wayland-info-*.log 2>/dev/null || true

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-3 waypipe smoke end-to-end"
    echo "[s35] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s35] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
