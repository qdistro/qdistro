#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier2-hardening.
#
# Spawns a tier-2 container and asserts the container-runtime
# isolation invariants set by tier2/spawn-tier2.sh. This is a
# regression-guard: if someone loosens a flag during debugging and
# forgets to revert, this driver fails loud.
#
# Asserted invariants (see tier2/spawn-tier2.sh "Hardening knobs"):
#   1. CapEff = 0           (--cap-drop=ALL)
#   2. NoNewPrivs = 1       (--security-opt=no-new-privileges)
#   3. Only `lo` interface  (--network=none)
#   4. Root mountinfo `ro`  (--read-only)
#   5. /run/user/<uid> empty of host secrets
#                            (per-container runtime dir, not bind of host)
#   6. Per-container dir ≤ outer wayland + pipewire + inner sockets
#                            (negative: no `bus`, no ssh-agent, no gnupg,
#                             no sibling tier-2 sockets)
#   7. Container has the qdistro_tier2_token label
#                            (orphan-dir reaper depends on this)
#
# Builds the tier-2 image on demand, so this driver can run either
# standalone or after s32 / s33 / s34 in the larger tiered suite.
# Missing build/runtime dependencies are hard failures here because this
# driver is the hardening regression guard for the tier-2 runtime.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
die() { fail "$*"; exit 1; }

SRC=/root/qdistro-src/qdistro
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
CONTAINER=tier2-c-harden
WORKLOAD=weston-terminal
IMAGE="qdistro/tier2-${WORKLOAD}:latest"

command -v podman >/dev/null 2>&1 || die "podman not installed in this VM"
[ -d "$TIER2_DIR" ] || die "tier2 source not unpacked at $TIER2_DIR"
[ -f "$COMMON_LIB_DIR/spawn-common.sh" ] || die "spawn-common library not unpacked at $COMMON_LIB_DIR"
if ! runuser -u admin -- podman image exists "$IMAGE" 2>/dev/null; then
    echo "[s40] building $IMAGE (first run; cached afterwards)..."
    if ! runuser -u admin -- bash "$TIER2_DIR/make-tier2-image.sh" "$WORKLOAD" >/tmp/s40-build.log 2>&1; then
        echo "--- build log ---" >&2
        cat /tmp/s40-build.log >&2
        die "build failed for $IMAGE -- see /tmp/s40-build.log"
    fi
fi

ADMIN_UID=1000
runuser -u admin -- test -S "/run/user/$ADMIN_UID/wayland-1" \
    || die "outer compositor not running"

# Broker: allow the tier-2 spawn gate for our workload. Since
# `security: require broker allow for tier2 spawns`, spawn-tier2 fails closed
# (decision=unknown) unless a rule allows qdistro.tier2.spawn:<workload>/<app>.
# This is the hardening regression guard, not a broker-policy test, so it
# authors its own allow rule exactly like the silo-secctx-wiretag probe.
RULE_DIR=/etc/qdistro/rules.d
RULE_FILE="$RULE_DIR/zz-tier2-hardening-allow.yaml"
SPAWN_ACTION="qdistro.tier2.spawn:${WORKLOAD}/${WORKLOAD}"
systemctl start qdistro-admin-broker.service 2>/dev/null || true
install -d -m 0755 "$RULE_DIR"
cat >"$RULE_FILE" <<EOF
# Test-authored: allow the tier-2 spawn of $WORKLOAD so the hardening lock-in
# lane can mint the container. s40-tier2-hardening.sh.
- name: tier2-hardening-${WORKLOAD}-allow
  decision: allow
  match:
    action: $SPAWN_ACTION
EOF
trap 'rm -f "$RULE_FILE" 2>/dev/null || true; systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true' EXIT
systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
broker_reply=""
for _ in $(seq 1 20); do
    broker_reply=$(runuser -u admin -- env XDG_RUNTIME_DIR="/run/user/$ADMIN_UID" \
        dbus-send --system --print-reply=literal \
        --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckPermission \
        "string:$SPAWN_ACTION" "dict:string:string:" 2>/dev/null | tr -d ' \t\n')
    [ "$broker_reply" = "allow" ] && break
    sleep 0.25
done
[ "$broker_reply" = "allow" ] \
    || die "broker did not load the tier-2 hardening allow rule (CheckPermission='$broker_reply')"

# Cleanup any leftover container with the same name.
runuser -u admin -- podman rm -f "$CONTAINER" >/dev/null 2>&1 || true

SPAWN_OUT=$(mktemp)
runuser -u admin -- env QDISTRO_PROFILE=dev bash "$TIER2_DIR/spawn-tier2.sh" \
    "$CONTAINER" "$WORKLOAD" -- weston-terminal \
    >"$SPAWN_OUT" 2>/tmp/s40-spawn.log &
SPAWN_PID=$!

# Wait for container to be up.
for _ in $(seq 1 30); do
    runuser -u admin -- podman ps --format '{{.Names}}' 2>/dev/null \
        | grep -qx "$CONTAINER" && break
    sleep 0.5
done

if ! runuser -u admin -- podman ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    fail "container $CONTAINER did not start within 15s"
    cat /tmp/s40-spawn.log >&2
    rm -f "$SPAWN_OUT"
    exit 1
fi
pass "container $CONTAINER running"

# Parsing is host-side because the minimal tier-2 image doesn't ship
# awk; we use grep + shell substitution against /proc files we cat
# out of the container.

# --- 1. CapEff = 0 ---
CAPEFF_LINE=$(runuser -u admin -- podman exec "$CONTAINER" \
    grep '^CapEff:' /proc/self/status 2>/dev/null)
CAPEFF=${CAPEFF_LINE##*	}
if [ "$CAPEFF" = "0000000000000000" ]; then
    pass "CapEff=0 (--cap-drop=ALL effective)"
else
    fail "CapEff=$CAPEFF — expected 0000000000000000 (--cap-drop=ALL not effective)"
fi

# --- 2. NoNewPrivs = 1 ---
NNP_LINE=$(runuser -u admin -- podman exec "$CONTAINER" \
    grep '^NoNewPrivs:' /proc/self/status 2>/dev/null)
NNP=${NNP_LINE##*	}
if [ "$NNP" = "1" ]; then
    pass "NoNewPrivs=1 (no-new-privileges effective)"
else
    fail "NoNewPrivs=$NNP — expected 1 (no-new-privileges not effective)"
fi

# --- 3. Only `lo` ---
NET_IFS=$(runuser -u admin -- podman exec "$CONTAINER" \
    ls /sys/class/net 2>/dev/null | tr '\n' ' ')
if [ "$(echo "$NET_IFS" | tr -d ' ')" = "lo" ]; then
    pass "network=none (only lo present)"
else
    fail "expected only 'lo' interface, got: $NET_IFS"
fi

# --- 4. Root mountinfo ro ---
# mountinfo: field 5 = mount point, field 6 = mount opts. Use awk to
# match $5 == "/" exactly so we don't accidentally pick up /dev or
# similar (the kernel's emit order isn't guaranteed). The container
# image lacks awk, so we cat mountinfo out and parse on the host.
ROOT_LINE=$(runuser -u admin -- podman exec "$CONTAINER" \
    cat /proc/self/mountinfo 2>/dev/null \
    | awk '$5 == "/" {print; exit}')
# shellcheck disable=SC2086 # intentional word-split for field index
set -- $ROOT_LINE
ROOT_MOUNT_OPTS="${6:-}"
case "$ROOT_MOUNT_OPTS" in
    ro,*|ro|*,ro,*|*,ro)
        pass "rootfs mounted read-only"
        ;;
    *)
        fail "rootfs mount opts '$ROOT_MOUNT_OPTS' don't include ro"
        ;;
esac

# Cross-check by trying a write.
if runuser -u admin -- podman exec "$CONTAINER" touch /no-such-write 2>/dev/null; then
    fail "touch / succeeded — read-only not enforced!"
    runuser -u admin -- podman exec "$CONTAINER" rm -f /no-such-write 2>/dev/null || true
else
    pass "touch / blocked by read-only"
fi

# --- 5+6. Runtime dir contains only allowed file types ---
# Allowed: wayland-* (outer + inner-tier2), pipewire-N(-manager)?(\.lock)?,
# tier2-weston-*.log, qdwin-nested-input-*.sock
RUNTIME_LIST=$(runuser -u admin -- podman exec "$CONTAINER" \
    ls /run/user/$ADMIN_UID/ 2>/dev/null)

DENIED=""
for entry in $RUNTIME_LIST; do
    case "$entry" in
        wayland-*|pipewire-[0-9]*|tier2-weston-*.log|qdwin-nested-input-*.sock)
            ;;
        *)
            DENIED="$DENIED $entry"
            ;;
    esac
done
if [ -z "$DENIED" ]; then
    pass "/run/user/$ADMIN_UID/ contains only allowed sockets/logs"
else
    fail "/run/user/$ADMIN_UID/ leaks host entries:$DENIED"
fi

# Negative checks: explicit disallow of well-known sensitive entries.
for danger in bus pulse gnupg ssh-agent.socket .dbus-keyrings; do
    if echo "$RUNTIME_LIST" | grep -qx "$danger"; then
        fail "container can see host $danger"
    fi
done
pass "no host bus/pulse/gnupg/ssh-agent in /run/user/$ADMIN_UID/"

# --- 7. Container has qdistro_tier2_token label ---
LABEL_TOKEN=$(runuser -u admin -- podman inspect "$CONTAINER" \
    --format '{{index .Config.Labels "qdistro_tier2_token"}}' 2>/dev/null)
if [ -n "$LABEL_TOKEN" ] && [ "$LABEL_TOKEN" != "<no value>" ]; then
    pass "qdistro_tier2_token label set ($LABEL_TOKEN)"
else
    fail "qdistro_tier2_token label missing — orphan-dir reaper will fail to identify live containers"
fi

# --- Cleanup --------------------------------------------------------------
runuser -u admin -- podman stop -t 2 "$CONTAINER" >/dev/null 2>&1 || true
wait "$SPAWN_PID" 2>/dev/null || true
rm -f "$SPAWN_OUT" /tmp/s40-spawn.log /tmp/s40-build.log 2>/dev/null || true

# --- Summary --------------------------------------------------------------
if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-2 hardening invariants enforced"
    echo "[s40] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s40] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
