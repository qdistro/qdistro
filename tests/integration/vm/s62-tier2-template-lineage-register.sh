#!/bin/bash
# In-VM driver: a binding-resolved TEMPLATED tier-2 silo, launched through the
# PRODUCTION systemd unit, auto-registers a launch record (02/S3c — the
# tier-2-template half of permission-lineage rollout).
#
# Companion to s60 (tier-3) and s61 (tier-2-podman, image-per-workload). The
# distinguishing feature here is that the silo identity is BINDING-RESOLVED from
# a promoted template, so `TIER2_SILO` is set and the launch record is keyed on
# the RESOLVED silo name (e.g. "silowt"), NOT the container name — proving the
# permission-lineage record carries the template-resolved silo identity.
#
# Production launch path (same as tier2-silo-secctx-wiretag-probe.sh):
#   qdistro-tier2-silo@<silo>.service (User=root)
#     -> qdistro-tier2-silo-launch (resolves the binding AS ADMIN)
#       -> spawn-tier2 in root-launcher mode (TIER2_ROOT_LAUNCHER=1, TIER2_SILO set)
#         -> qd_register_secctx_launch_record -> broker RegisterLaunch
#
# Load-bearing assertions:
#   1. spawn-tier2 (via the silo unit) emitted "[tier2] lineage: registered
#      pid=<n> as silo=<silo>" in the unit journal, with silo == the
#      binding-resolved silo name (NOT the container name).
#   2. the broker wrote a qdistro.lineage.register:<silo> audit row.
#
# Reuses the wiretag probe's near-instant FROM-only recipe (re-tags the shipped
# weston-terminal tier-2 image under a promoted generation digest) so the silo
# is genuinely binding-resolved. lineage_enforce is NOT toggled: the record is
# written unconditionally; this driver only proves it is produced + persisted.

set -u

PASSCOUNT=0
FAILCOUNT=0
pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SRC=/root/qdistro-src/qdistro
SPAWN=/usr/bin/qdistro-tier2-spawn
LAUNCH_UNIT_TMPL="qdistro-tier2-silo@"
LAUNCH_HELPER=/usr/libexec/qdistro/qdistro-tier2-silo-launch
STOP_HELPER=/usr/libexec/qdistro/qdistro-tier2-silo-stop
ADMIN=admin
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER=wayland-1
WORKLOAD=weston-terminal
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
RULE_DIR=/etc/qdistro/rules.d
RULE_FILE="$RULE_DIR/zz-s62-template-lineage-allow.yaml"
TIER2_BUILD_DIR=/tmp/qd-s62-tier2
LAUNCH_ENV_DIR=/run/qdistro/silo-launch
RECIPES_DIR=/usr/lib/qdistro/templates/recipes
ETC_TEMPLATES=/etc/qdistro/templates
AUDIT_DB=/var/lib/qdistro/audit/audit.sqlite
SILO=s62silo
TEMPLATE=s62silo
CONTAINERFILE_NAME=Containerfile.s62silo
TRAP_FIRED=0

as_admin() { runuser -u "$ADMIN" -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" "$@"; }
broker_check_admin() {
    as_admin dbus-send --system --print-reply=literal \
        --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckPermission \
        "string:$1" "dict:string:string:" 2>/dev/null | tr -d ' \t\n'
}
clean_silo() {
    systemctl stop "${LAUNCH_UNIT_TMPL}${SILO}.service" >/dev/null 2>&1 || true
    systemctl reset-failed "${LAUNCH_UNIT_TMPL}${SILO}.service" >/dev/null 2>&1 || true
    as_admin podman rm -f "qdistro-silo-${SILO}" >/dev/null 2>&1 || true
    rm -f "$LAUNCH_ENV_DIR/${SILO}.env" 2>/dev/null || true
}
write_launch_env() {
    # Mirror the daemon's _export_tier2_launch_env (single-quoted KEY='VALUE').
    # The daemon always sets a non-empty template_silo, so TIER2_SILO == the
    # binding-resolved silo name. Root-owned 0600 or the helpers refuse it.
    install -d -m 0755 "$LAUNCH_ENV_DIR"
    local f="$LAUNCH_ENV_DIR/${SILO}.env"
    cat >"$f" <<EOF
TIER2_SILO='${SILO}'
TIER2_NETWORK='none'
QD_WORKLOAD='${WORKLOAD}'
QD_CONTAINER='qdistro-silo-${SILO}'
QD_APP_ARGV_JSON='["${WORKLOAD}"]'
EOF
    chown 0:0 "$f"; chmod 0600 "$f"
}
cleanup() {
    [ "$TRAP_FIRED" -eq 1 ] && return 0
    TRAP_FIRED=1
    clean_silo
    rm -f "$RULE_FILE" "$RECIPES_DIR/$CONTAINERFILE_NAME" \
          "$ETC_TEMPLATES/$TEMPLATE.toml" \
          "/var/lib/qdistro/bindings/${SILO}.toml" \
          "/var/lib/qdistro/bindings/${SILO}.activated" \
          "/run/qdistro/silo-generation/${SILO}" 2>/dev/null || true
    # The promote materializes a persistent silo STATE TREE; remove it (and the
    # staged build dir + logs) like the wiretag probe's teardown, else it leaks.
    rm -rf "/var/lib/qdistro/silos/${SILO}" "$TIER2_BUILD_DIR" 2>/dev/null || true
    rm -f /tmp/s62-build.log /tmp/s62-tbuild.log /tmp/s62-tvalidate.log \
          /tmp/s62-tpromote.log /tmp/s62-unitstart.log 2>/dev/null || true
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- 1. prerequisites ------------------------------------------------
command -v podman    >/dev/null 2>&1 || skip "podman not installed"
command -v dbus-send >/dev/null 2>&1 || skip "dbus-send absent"
command -v runuser   >/dev/null 2>&1 || skip "runuser absent"
command -v sqlite3   >/dev/null 2>&1 || skip "sqlite3 absent"
command -v qdistro-secctx-exec   >/dev/null 2>&1 || skip "qdistro-secctx-exec not installed"
command -v qdistro-template-build >/dev/null 2>&1 || skip "qdistro-template-build not in PATH (templates not installed)"
[ -x "$SPAWN" ] || skip "$SPAWN not installed"
[ -x "$LAUNCH_HELPER" ] || skip "$LAUNCH_HELPER not installed (silo launch helper absent)"
[ -x "$STOP_HELPER" ]   || skip "$STOP_HELPER not installed"
[ -f "/etc/systemd/system/${LAUNCH_UNIT_TMPL}.service" ] || skip "silo launcher unit not installed"
[ -f "$AUDIT_DB" ] || skip "broker audit db missing at $AUDIT_DB"
[ -d "$SRC/tier2" ] || skip "tier2 source not unpacked at $SRC/tier2"
grep -qx 'User=root' "/etc/systemd/system/${LAUNCH_UNIT_TMPL}.service" \
    || skip "silo unit is not User=root (predates root-launcher wiring)"
as_admin test -S "$RUNTIME_DIR/$OUTER" || skip "outer admin compositor not up ($RUNTIME_DIR/$OUTER missing)"
pass "tier2-template prerequisites present"

systemctl start qdistro-admin-broker.service 2>/dev/null || true

# --- 2. build the weston-terminal tier-2 image (cached) --------------
rm -rf "$TIER2_BUILD_DIR" 2>/dev/null || true
cp -r "$SRC/tier2" "$TIER2_BUILD_DIR" || skip "stage tier2 build dir"
chmod -R a+rX "$TIER2_BUILD_DIR"; find "$TIER2_BUILD_DIR" -name '*.sh' -exec chmod a+rx {} +
if ! as_admin podman image exists "$IMAGE" 2>/dev/null; then
    as_admin bash "$TIER2_BUILD_DIR/make-tier2-image.sh" "$WORKLOAD" >/tmp/s62-build.log 2>&1 \
        || { cat /tmp/s62-build.log >&2; skip "build of $IMAGE failed — see /tmp/s62-build.log"; }
fi
as_admin podman image exists "$IMAGE" || skip "$IMAGE absent after build"
pass "tier-2 image present"

# --- 3. drop a FROM-only recipe + template ---------------------------
install -d -m 0755 "$RECIPES_DIR" "$ETC_TEMPLATES"
cat >"$RECIPES_DIR/$CONTAINERFILE_NAME" <<EOF
# Test-only recipe (s62-tier2-template-lineage): FROM the shipped weston-terminal
# tier-2 image so the promoted generation is a real wayland client, no pkg work.
FROM $IMAGE
CMD ["weston-terminal"]
EOF
cat >"$ETC_TEMPLATES/$TEMPLATE.toml" <<EOF
[template]
class = "derived"

[template.state_boundary]
class = "recipe-derived-toolchain"
enforced = "true"

[template.build]
containerfile = "$CONTAINERFILE_NAME"
network_mode = "unrestricted"

[[template.probe]]
name = "process-starts"
kind = "process"
class = "local-runtime"
command = "true"
timeout = 30
EOF

# --- 4. author the broker spawn allow rule + settle ------------------
install -d -m 0755 "$RULE_DIR"
cat >"$RULE_FILE" <<EOF
# Test-authored (s62-tier2-template-lineage): allow the tier-2 silo spawn so the
# binding-resolved template lineage-register path can launch.
- name: s62-template-lineage-spawn-allow
  decision: allow
  match:
    action: qdistro.tier2.spawn:${WORKLOAD}/${WORKLOAD}
- name: s62-template-lineage-nested-advertise-allow
  decision: allow
  match:
    action: qdistro.nested.advertise:org.freedesktop.weston.wayland-terminal
EOF
systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
# Settle on BOTH authored actions (spawn gate + nested-advertise gate) so the
# evidence proves both allows are live, not just that the file parsed.
SPAWN_GATE="qdistro.tier2.spawn:${WORKLOAD}/${WORKLOAD}"
ADVERTISE_GATE="qdistro.nested.advertise:org.freedesktop.weston.wayland-terminal"
r1=""; r2=""
for _ in $(seq 1 20); do
    r1=$(broker_check_admin "$SPAWN_GATE")
    r2=$(broker_check_admin "$ADVERTISE_GATE")
    [ "$r1" = allow ] && [ "$r2" = allow ] && break
    sleep 0.25
done
if [ "$r1" = allow ] && [ "$r2" = allow ]; then
    pass "broker spawn gate allows qdistro.tier2.spawn:${WORKLOAD}/${WORKLOAD}"
else
    fail "broker did not load the tier-2 lineage rules (spawn=$r1 advertise=$r2)"
fi

# --- 5. build + validate + promote the silo into the PRODUCTION tree -
build_out=$(as_admin qdistro-template-build "$TEMPLATE" 2>/tmp/s62-tbuild.log) \
    || { cat /tmp/s62-tbuild.log >&2; fail "template-build failed"; }
rid=$(printf '%s\n' "$build_out" | sed -n 's/^RUN_ID=//p' | head -1)
[ -n "$rid" ] || { printf '%s\n' "$build_out" >&2; fail "no RUN_ID from build"; }
if [ -n "${rid:-}" ]; then
    as_admin qdistro-template-validate "$rid" >/tmp/s62-tvalidate.log 2>&1 \
        || { cat /tmp/s62-tvalidate.log >&2; fail "validate failed for $rid"; }
    as_admin qdistro-template-promote "$SILO" "$rid" >/tmp/s62-tpromote.log 2>&1 \
        || { cat /tmp/s62-tpromote.log >&2; fail "promote failed for $SILO/$rid"; }
    [ -f "/var/lib/qdistro/bindings/${SILO}.toml" ] \
        && pass "silo '$SILO' binding-resolved + promoted into the production tree" \
        || fail "no binding at /var/lib/qdistro/bindings/${SILO}.toml after promote"
fi

# --- 6. baseline audit count for the RESOLVED silo -------------------
clean_silo
LINEAGE_ACTION="qdistro.lineage.register:${SILO}"
BEFORE=$(sqlite3 "$AUDIT_DB" \
    "SELECT count(*) FROM audit WHERE action='${LINEAGE_ACTION}';" 2>/dev/null || echo 0)

# --- 7. launch via the PRODUCTION silo systemd unit ------------------
write_launch_env
UNIT="${LAUNCH_UNIT_TMPL}${SILO}.service"
CURSOR=$(journalctl --since=now -n0 --show-cursor 2>/dev/null | awk -F': ' '/-- cursor:/ {print $2}')
# A non-empty cursor is the clean invariant that the registration line we match
# below belongs to THIS launch's unit journal, not a stale prior row.
[ -n "$CURSOR" ] || fail "could not capture a journal cursor before launch"
systemctl start "$UNIT" 2>/tmp/s62-unitstart.log || true

# --- 8. wait for the lineage registration line in the unit journal ---
REG_SILO=""
for _ in $(seq 1 60); do
    jl=$(journalctl -u "$UNIT" ${CURSOR:+--after-cursor="$CURSOR"} 2>/dev/null \
         | grep -m1 "lineage: registered pid=[0-9]\+ as silo=" || true)
    if [ -n "$jl" ]; then
        REG_SILO=$(echo "$jl" | sed -n 's/.*as silo=\([^ ]*\).*/\1/p')
        break
    fi
    if journalctl -u "$UNIT" ${CURSOR:+--after-cursor="$CURSOR"} 2>/dev/null | grep -q "WARN lineage:"; then
        journalctl -u "$UNIT" ${CURSOR:+--after-cursor="$CURSOR"} 2>/dev/null | tail -30 >&2
        fail "silo unit emitted a WARN lineage line (launch record not registered)"
        break
    fi
    sleep 0.5
done

if [ -n "$REG_SILO" ]; then
    pass "silo unit (root-launcher) logged the launch-record registration for silo=$REG_SILO"
    # The binding-resolved silo identity (TIER2_SILO=$SILO) MUST be what gets
    # registered — NOT the container name qdistro-silo-$SILO. That distinction
    # is the whole point of the template path.
    if [ "$REG_SILO" != "$SILO" ]; then
        fail "registered silo '$REG_SILO' != binding-resolved silo '$SILO' (lineage not keyed on the resolved identity)"
    else
        pass "lineage record keyed on the binding-resolved silo identity ($SILO), not the container name"
    fi
else
    [ "$FAILCOUNT" -eq 0 ] && { journalctl -u "$UNIT" ${CURSOR:+--after-cursor="$CURSOR"} 2>/dev/null | tail -40 >&2; \
        fail "silo unit did not register a launch record within 30s"; }
fi

# --- 9. broker wrote the register audit row --------------------------
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
if [ "$FAILCOUNT" -eq 0 ] && [ "$PASSCOUNT" -ge 6 ]; then
    pass "§02/S3c tier-2 (binding-resolved template) permission-lineage register end-to-end"
    echo "[s62] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s62] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
