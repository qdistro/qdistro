#!/bin/bash
# In-VM driver: spawn-tier2.sh (root-launcher mode) auto-registers a launch
# record (02/S3b — the tier-2-podman half of permission-lineage rollout).
#
# Companion to s60-tier3-lineage-register.sh (tier-3). Proves the registration
# half of permission lineage for tier-2: when a tier-2 silo is launched via the
# root-launcher path (TIER2_ROOT_LAUNCHER=1 — the production topology, which
# spawn-tier2.sh notes "selects the proven tier-3 topology"), spawn-tier2 reads
# the inner pid published by qdistro-secctx-exec and calls the broker's
# RegisterLaunch via the shared qd_register_secctx_launch_record helper, binding
# (pid,starttime) -> silo. A later cross-silo gate then resolves the source pid
# qdwin reports to that record so enforce mode can ALLOW an admin-authored
# cross-silo rule instead of failing closed.
#
# Load-bearing assertions:
#   1. spawn-tier2 (root-launcher) emitted "[tier2] lineage: registered
#      pid=<n> as silo=<silo>".
#   2. the broker wrote a qdistro.lineage.register:<silo> audit row (the
#      RegisterLaunch re-verified the live pid and stored the record).
#
# The broker spawn gate (qdistro.tier2.spawn:<workload>/<app>) is rules-only /
# fail-closed; this driver authors the allow rule itself (idempotent with
# tiered-isolation.bats setup_file) so it also runs standalone like s32/s60.
#
# Pairs with s32 (tier-2 bring-up) and s60 (tier-3 lineage). lineage_enforce is
# NOT toggled here: the launch record is written UNCONDITIONALLY (so enforce can
# be flipped without relaunching apps), and this driver only needs to prove the
# record is produced + persisted, not the enforce decision.

set -u

PASSCOUNT=0
FAILCOUNT=0
pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

# --- config ----------------------------------------------------------
SRC=/root/qdistro-src/qdistro
TIER2_DIR=/tmp/qdistro-tier2-lineage
COMMON_LIB_DIR=/tmp/lib
ADMIN_USER=admin
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER=wayland-1
WORKLOAD=weston-terminal
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
SILO_NAME="lineage-c1"
CONTAINER="qdistro-silo-${SILO_NAME}"
AUDIT_DB=/var/lib/qdistro/audit/audit.sqlite
RULE_DIR=/etc/qdistro/rules.d
RULE_FILE="$RULE_DIR/zz-s61-tier2-lineage-allow.yaml"
SPAWN_PID=""
TRAP_FIRED=0

cleanup() {
    [ "$TRAP_FIRED" -eq 1 ] && return 0
    TRAP_FIRED=1
    [ -n "$SPAWN_PID" ] && kill -TERM "$SPAWN_PID" 2>/dev/null || true
    [ -n "$SPAWN_PID" ] && wait    "$SPAWN_PID" 2>/dev/null || true
    runuser -u "$ADMIN_USER" -- podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -f "$RULE_FILE" 2>/dev/null || true
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    rm -f /tmp/s61-spawn.log 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- 1. prerequisites ------------------------------------------------
command -v podman   >/dev/null 2>&1 || skip "podman not installed in this VM"
command -v sqlite3  >/dev/null 2>&1 || skip "sqlite3 not available"
command -v dbus-send >/dev/null 2>&1 || skip "dbus-send absent (broker gate unqueryable)"
command -v qdistro-secctx-exec >/dev/null 2>&1 || skip "qdistro-secctx-exec not installed"
[ -d "$SRC/tier2" ] || skip "tier2 source not unpacked at $SRC/tier2"
[ -f "$AUDIT_DB" ]  || skip "broker audit db missing at $AUDIT_DB"
rm -rf "$TIER2_DIR" 2>/dev/null || true
cp -r "$SRC/tier2" "$TIER2_DIR" || skip "could not stage tier2 build dir"
chmod -R a+rX "$TIER2_DIR"; find "$TIER2_DIR" -name '*.sh' -exec chmod a+rx {} +
if [ -d "$SRC/lib" ]; then
    rm -rf "$COMMON_LIB_DIR" 2>/dev/null || true
    cp -r "$SRC/lib" "$COMMON_LIB_DIR"; chmod -R a+rX "$COMMON_LIB_DIR"
fi
SPAWN="$TIER2_DIR/spawn-tier2.sh"
[ -f "$SPAWN" ] || skip "spawn-tier2.sh missing at $SPAWN"
if ! runuser -u "$ADMIN_USER" -- test -S "$RUNTIME_DIR/$OUTER"; then
    skip "outer admin compositor not up ($RUNTIME_DIR/$OUTER missing)"
fi
pass "tier2 prerequisites present"

# --- 2. build the tier-2 image on demand (cached) --------------------
if ! runuser -u "$ADMIN_USER" -- podman image exists "$IMAGE" 2>/dev/null; then
    if ! runuser -u "$ADMIN_USER" -- bash "$TIER2_DIR/make-tier2-image.sh" "$WORKLOAD" \
            >/tmp/s61-build.log 2>&1; then
        cat /tmp/s61-build.log >&2
        skip "build of $IMAGE failed — see /tmp/s61-build.log"
    fi
fi
runuser -u "$ADMIN_USER" -- podman image exists "$IMAGE" || skip "$IMAGE absent after build"
pass "tier-2 image present"

# --- 3. author the broker spawn allow rule + settle ------------------
# Rules-only / fail-closed: without this rule the root-launcher spawn is
# refused at the broker gate. Idempotent with tiered-isolation.bats setup_file.
systemctl start qdistro-admin-broker.service 2>/dev/null || true
install -d -m 0755 "$RULE_DIR"
cat >"$RULE_FILE" <<YAML
# Test-authored (s61-tier2-lineage-register.sh): allow the tier-2
# weston-terminal spawn so the lineage-register evidence path can launch.
- name: s61-tier2-lineage-spawn-allow
  decision: allow
  match:
    action: qdistro.tier2.spawn:${WORKLOAD}/${WORKLOAD}
- name: s61-tier2-lineage-nested-advertise-allow
  decision: allow
  match:
    action: qdistro.nested.advertise:org.freedesktop.weston.wayland-terminal
YAML
systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
bc() {
    runuser -u "$ADMIN_USER" -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        dbus-send --system --print-reply=literal \
        --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckPermission \
        "string:$1" "dict:string:string:" 2>/dev/null | tr -d ' \t\n'
}
# Settle on BOTH authored actions (the root-launcher spawn hits the spawn gate
# AND the in-container publisher hits the nested-advertise gate) so the evidence
# proves both allows are actually live, not just that the file parsed.
SPAWN_GATE="qdistro.tier2.spawn:${WORKLOAD}/${WORKLOAD}"
ADVERTISE_GATE="qdistro.nested.advertise:org.freedesktop.weston.wayland-terminal"
r1=""; r2=""
for _ in $(seq 1 20); do
    r1=$(bc "$SPAWN_GATE")
    r2=$(bc "$ADVERTISE_GATE")
    [ "$r1" = allow ] && [ "$r2" = allow ] && break
    sleep 0.25
done
if [ "$r1" = allow ] && [ "$r2" = allow ]; then
    pass "broker spawn gate allows ${SPAWN_GATE}"
else
    fail "broker did not load the tier-2 lineage rules (spawn=$r1 advertise=$r2)"
fi

# --- 4. baseline audit count for this silo ---------------------------
runuser -u "$ADMIN_USER" -- podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
LINEAGE_ACTION="qdistro.lineage.register:${CONTAINER}"
BEFORE=$(sqlite3 "$AUDIT_DB" \
    "SELECT count(*) FROM audit WHERE action='${LINEAGE_ACTION}';" 2>/dev/null || echo 0)

# --- 5. spawn the tier-2 silo via the ROOT-LAUNCHER path -------------
# TIER2_ROOT_LAUNCHER=1 runs secctx-exec AS ADMIN under runuser (the proven
# tier-3 topology) and exports the launch-record path, so the shared helper
# registers the inner pid with the broker. We background it and wait for the
# registration line, then the trap stops the container.
SPAWN_LOG=/tmp/s61-spawn.log
: >"$SPAWN_LOG"
env TIER2_ROOT_LAUNCHER=1 TIER2_ADMIN_UID="$ADMIN_UID" \
    WAYLAND_DISPLAY="$OUTER" \
    bash "$SPAWN" "$CONTAINER" "$WORKLOAD" -- "$WORKLOAD" >"$SPAWN_LOG" 2>&1 &
SPAWN_PID=$!

# --- 6. wait for the lineage registration line -----------------------
REG_SILO=""
for _ in $(seq 1 60); do
    line=$(grep -m1 "lineage: registered pid=[0-9]\+ as silo=" "$SPAWN_LOG" 2>/dev/null || true)
    if [ -n "$line" ]; then
        REG_SILO=$(echo "$line" | sed -n 's/.*as silo=\([^ ]*\).*/\1/p')
        break
    fi
    if grep -q "WARN lineage:" "$SPAWN_LOG" 2>/dev/null; then
        cat "$SPAWN_LOG" >&2
        fail "spawn-tier2 emitted a WARN lineage line (launch record not registered)"
        break
    fi
    # spawn died early?
    if ! kill -0 "$SPAWN_PID" 2>/dev/null && ! grep -q "lineage: registered" "$SPAWN_LOG" 2>/dev/null; then
        cat "$SPAWN_LOG" >&2
        fail "spawn-tier2 exited before registering a launch record"
        break
    fi
    sleep 0.5
done

if [ -n "$REG_SILO" ]; then
    pass "spawn-tier2 (root-launcher) logged the launch-record registration for silo=$REG_SILO"
    # The registered silo MUST be the container we launched. Guard against a
    # false-pass where some OTHER silo's pre-existing rows would satisfy a
    # mismatched baseline: LINEAGE_ACTION stays keyed to $CONTAINER (the same
    # action $BEFORE was counted for), and we assert the registered silo equals
    # it rather than silently re-keying to whatever was parsed.
    if [ "$REG_SILO" != "$CONTAINER" ]; then
        fail "registered silo '$REG_SILO' != launched container '$CONTAINER' (lineage bound to the wrong silo)"
    fi
else
    [ "$FAILCOUNT" -eq 0 ] && { cat "$SPAWN_LOG" >&2; fail "spawn-tier2 did not register a launch record within 30s"; }
fi

# --- 7. broker wrote the register audit row --------------------------
if [ -n "$REG_SILO" ]; then
    AFTER=0
    for _ in $(seq 1 20); do
        AFTER=$(sqlite3 "$AUDIT_DB" \
            "SELECT count(*) FROM audit WHERE action='${LINEAGE_ACTION}';" 2>/dev/null || echo 0)
        [ "$AFTER" -gt "$BEFORE" ] 2>/dev/null && break
        sleep 0.25
    done
    if [ "$AFTER" -gt "$BEFORE" ] 2>/dev/null; then
        pass "broker recorded ${LINEAGE_ACTION} (before=$BEFORE after=$AFTER)"
    else
        fail "no new ${LINEAGE_ACTION} audit row (before=$BEFORE after=$AFTER)"
    fi
fi

# --- summary ---------------------------------------------------------
if [ "$FAILCOUNT" -eq 0 ] && [ "$PASSCOUNT" -ge 5 ]; then
    pass "§02/S3b tier-2 (root-launcher) permission-lineage register end-to-end"
    echo "[s61] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s61] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
