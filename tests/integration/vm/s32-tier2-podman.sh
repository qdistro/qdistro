#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier2-podman.
#
# Brings up a single tier-2 container running weston-terminal, verifies
# that the inner xdg_toplevel is advertised through qdwin's nested-mode
# publisher path and surfaces as a chromed peer toplevel on the outer.
#
# Each scenario echoes "PASS: <description>" on success or
# "FAIL: <description>" on failure. The bats wrapper greps for the
# exact PASS strings.
#
# Skips with `SKIP:` when prereqs are absent — bats translates SKIP
# into fail_loud so the operator notices the missing infra.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

# --- Source unpacked by fresh-vm-bootstrap.sh -----------------------------
SRC=/root/qdistro-src/qdistro
# Stage tier2/ scripts under /tmp where the unprivileged admin user can
# reach them — /root is mode 0700 on the test VM.
TIER2_DIR=/tmp/qdistro-tier2
COMMON_LIB_DIR=/tmp/lib
if [ -d "$SRC/tier2" ]; then
    rm -rf "$TIER2_DIR" 2>/dev/null || true
    cp -r "$SRC/tier2" "$TIER2_DIR"
    chmod -R a+rX "$TIER2_DIR"
    find "$TIER2_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
if [ -d "$SRC/lib" ]; then
    rm -rf "$COMMON_LIB_DIR" 2>/dev/null || true
    cp -r "$SRC/lib" "$COMMON_LIB_DIR"
    chmod -R a+rX "$COMMON_LIB_DIR"
fi
CONTAINER=tier2-c1
WORKLOAD=weston-terminal
IMAGE="qdistro/tier2-${WORKLOAD}:latest"

# --- Prereqs --------------------------------------------------------------
command -v podman >/dev/null 2>&1 || skip "podman not installed in this VM"
[ -d "$TIER2_DIR" ] || skip "tier2 source not unpacked at $TIER2_DIR"
[ -f "$COMMON_LIB_DIR/spawn-common.sh" ] || skip "spawn-common library not unpacked at $COMMON_LIB_DIR"

# Outer compositor running? qdwin-compositor.service + qdshell user
# units are how the test VM brings up qdwin (see install-qdwin-session).
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER_SOCK="$RUNTIME_DIR/wayland-1"

if ! runuser -u admin -- test -S "$OUTER_SOCK"; then
    fail "outer admin compositor not up (wayland-1 socket missing)"
    skip "outer compositor not running; cannot proceed"
fi
pass "outer admin compositor up"

# --- Build image on demand (cached) ---------------------------------------
if ! runuser -u admin -- podman image exists "$IMAGE" 2>/dev/null; then
    echo "[s32] building $IMAGE (first run; cached afterwards)..."
    if ! runuser -u admin -- bash "$TIER2_DIR/make-tier2-image.sh" "$WORKLOAD" >/tmp/s32-build.log 2>&1; then
        echo "--- build log ---" >&2
        cat /tmp/s32-build.log >&2
        skip "build failed for $IMAGE — see /tmp/s32-build.log"
    fi
fi

# --- Spawn container ------------------------------------------------------
# Reset any leftover container with the same name.
runuser -u admin -- podman rm -f "$CONTAINER" >/dev/null 2>&1 || true

# Capture journal cursor before spawn so we only assert on lines emitted
# during this run.
JCURSOR_FILE=$(mktemp)
journalctl --since=now -n0 --show-cursor >"$JCURSOR_FILE" 2>/dev/null || true
CURSOR=$(awk -F': ' '/-- cursor:/ {print $2}' "$JCURSOR_FILE")

# Spawn detached so this driver retains control. spawn-tier2.sh emits
# correlation lines (LAUNCH_TOKEN=, CONTAINER=, IMAGE=, APP_ID=) to
# stdout before exec'ing podman.
SPAWN_OUT=$(mktemp)
# (no detach — caller backgrounds the whole spawn)
    runuser -u admin -- env QDISTRO_PROFILE=dev bash "$TIER2_DIR/spawn-tier2.sh" \
        "$CONTAINER" "$WORKLOAD" -- weston-terminal \
        >"$SPAWN_OUT" 2>/tmp/s32-spawn.log &
SPAWN_PID=$!

LAUNCH_TOKEN=""
for _ in $(seq 1 30); do
    LAUNCH_TOKEN=$(awk -F= '/^LAUNCH_TOKEN=/{print $2; exit}' "$SPAWN_OUT" 2>/dev/null)
    [ -n "$LAUNCH_TOKEN" ] && break
    sleep 0.5
done

if [ -z "$LAUNCH_TOKEN" ]; then
    cat /tmp/s32-spawn.log >&2 || true
    fail "spawn-tier2 did not emit LAUNCH_TOKEN within 15s"
fi

# Wait for container.
for _ in $(seq 1 30); do
    if runuser -u admin -- podman ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
        pass "container '$CONTAINER' running"
        break
    fi
    sleep 0.5
done
runuser -u admin -- podman ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER" \
    || fail "container '$CONTAINER' did not appear in 'podman ps' within 15s"

# --- Wait for outer journal lines ----------------------------------------
# qdwin emits "qdwin: nested-toplevel advertise pw_node='...' input_sink='...'
# app_id=... title=... origin_uid=..." once it accepts an advertise_toplevel
# request from the in-container publisher.
deadline=$(( $(date +%s) + 30 ))
ADVERTISE_LINE=""
while [ "$(date +%s)" -lt "$deadline" ]; do
    if [ -n "$CURSOR" ]; then
        ADVERTISE_LINE=$(journalctl --after-cursor="$CURSOR" 2>/dev/null \
            | grep -m1 "qdwin: nested-toplevel advertise" || true)
    else
        ADVERTISE_LINE=$(journalctl --since="-1min" 2>/dev/null \
            | grep -m1 "qdwin: nested-toplevel advertise" || true)
    fi
    [ -n "$ADVERTISE_LINE" ] && break
    sleep 0.5
done

if [ -n "$ADVERTISE_LINE" ]; then
    pass "container's weston-terminal advertised through the nested-mode publisher"

    # origin_uid=1000 confirms --userns=keep-id mapped the container's
    # process uid to admin's uid on the host.
    if echo "$ADVERTISE_LINE" | grep -q "origin_uid=1000"; then
        pass "origin_uid=1000 (--userns=keep-id mapping verified)"
    else
        fail "origin_uid not 1000 in advertise line: $ADVERTISE_LINE"
    fi
else
    fail "no 'qdwin: nested-toplevel advertise' line within 30s"
fi

# Outer qdwin/shell creates the chromed proxy toplevel — log line is
# "qdwin/nested-proxy: created handle=... uid=..." emitted from
# surface_added when the proxy gets a shell handle.
PROXY_LINE=""
deadline=$(( $(date +%s) + 15 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    if [ -n "$CURSOR" ]; then
        PROXY_LINE=$(journalctl --after-cursor="$CURSOR" 2>/dev/null \
            | grep -m1 "qdwin/nested-proxy: created handle=" || true)
    else
        PROXY_LINE=$(journalctl --since="-1min" 2>/dev/null \
            | grep -m1 "qdwin/nested-proxy: created handle=" || true)
    fi
    [ -n "$PROXY_LINE" ] && break
    sleep 0.5
done

if [ -n "$PROXY_LINE" ]; then
    pass "outer admin shell created chromed proxy for in-container app"
else
    fail "no 'qdwin/nested-proxy: created handle=' line within 15s"
fi

# qdshell spawns qdistro-nested-pixelfeed per inner toplevel. The
# consumer process is the wire from nested_proxy_pixel_source →
# bind_proxy_pixels; without it the proxy view stays on the placeholder
# curtain. Match running process first, fall back to a journal grep
# (in case the consumer exited cleanly between toplevel-add and assert).
# Poll: the nested-proxy journal line can land before qdshell starts the
# pixelfeed process (race under host load); a single-shot check flakes.
PIXEL_OK=0
pixel_deadline=$(( $(date +%s) + 20 ))
while [ "$(date +%s)" -lt "$pixel_deadline" ]; do
    if pgrep -f qdistro-nested-pixelfeed >/dev/null 2>&1; then
        PIXEL_OK=1
        break
    fi
    if [ -n "$CURSOR" ] && journalctl --after-cursor="$CURSOR" 2>/dev/null \
            | grep -q "spawning pixelfeed\|qdistro-nested-pixelfeed\|bind_proxy_pixels"; then
        PIXEL_OK=2
        break
    fi
    sleep 0.5
done
if [ "$PIXEL_OK" = "1" ]; then
    pass "qdshell spawned pixelfeed for in-container toplevel"
elif [ "$PIXEL_OK" = "2" ]; then
    pass "qdshell spawned pixelfeed for in-container toplevel (journal-confirmed)"
else
    fail "qdshell did not spawn qdistro-nested-pixelfeed for inner toplevel — check Qdwin.qml onNestedProxyPixelSource handler is firing (qdwin_shell_v1 v9+)"
fi

# --- Cleanup --------------------------------------------------------------
runuser -u admin -- podman stop -t 2 "$CONTAINER" >/dev/null 2>&1 || true
wait "$SPAWN_PID" 2>/dev/null || true
rm -f "$SPAWN_OUT" "$JCURSOR_FILE" /tmp/s32-spawn.log /tmp/s32-build.log 2>/dev/null || true

# --- Summary --------------------------------------------------------------
if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-2 podman + nested compositor end-to-end"
    echo "[s32] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s32] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
