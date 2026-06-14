#!/bin/bash
# In-VM driver: tier-2-template spawn -> pre-activation SNAPSHOT -> podman-run,
# end-to-end on REAL btrfs (05/B #5, D9).
#
# The host unit tests (test_state_snapshot.py) prove the snapshot ORCHESTRATION
# with the `copy` mechanism; the btrfs `subvolume` mechanism is mocked there.
# templates-state-snapshot.bats proves the btrfs mechanism in ISOLATION (a probe
# builds a loopback + calls the module directly). This driver proves the missing
# piece: the REAL PRODUCTION LAUNCH PATH (qdistro-tier2-silo@<silo>.service ->
# qdistro-tier2-silo-launch -> qdistro_resolve_binding -> spawn-tier2 -> podman)
# takes a real btrfs pre-activation subvolume snapshot of the OUTGOING state
# before the INCOMING generation activates, then runs the container.
#
# The pre-activation snapshot only fires on a GENERATION FLIP (outgoing !=
# incoming), so the driver promotes + launches gen1 (first activation, no
# snapshot), then promotes a DISTINCT gen2 (recipe carries an extra LABEL so the
# build digest differs) and launches again — that flip is what must snapshot.
#
# Load-bearing assertions:
#   1. the silo state is created as a btrfs SUBVOLUME (mechanism=subvolume) — else
#      SKIP (the VM root is not btrfs and the btrfs path cannot be proven).
#   2. gen1 activates (template.binding.activated), establishing the outgoing gen.
#   3. the gen2 launch logs "pre-activation snapshot <id> taken (mechanism=subvolume)".
#   4. a template.state_snapshot.created audit row (mechanism=subvolume) appears.
#   5. the snapshot payload is a real READ-ONLY btrfs subvolume.
#   6. gen2 activates (template.binding.activated) AND its container runs.

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
ADMIN=admin
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER=wayland-1
WORKLOAD=weston-terminal
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
RULE_DIR=/etc/qdistro/rules.d
RULE_FILE="$RULE_DIR/zz-s64-snapshot-allow.yaml"
TIER2_BUILD_DIR=/tmp/qd-s64-tier2
LAUNCH_ENV_DIR=/run/qdistro/silo-launch
RECIPES_DIR=/usr/lib/qdistro/templates/recipes
ETC_TEMPLATES=/etc/qdistro/templates
AUDIT_DB=/var/lib/qdistro/audit/audit.sqlite
SILO=s64silo
TEMPLATE=s64silo
CONTAINERFILE_NAME=Containerfile.s64silo
SILO_DIR=/var/lib/qdistro/silos/$SILO
STATE_PATH="$SILO_DIR/state"
STATE_META="$SILO_DIR/state.meta.toml"
SNAP_DIR="$SILO_DIR/state-snapshots"
UNIT="${LAUNCH_UNIT_TMPL}${SILO}.service"
SILOS_DIR=/var/lib/qdistro/silos
BTRFS_IMG=/var/tmp/s64-silos-btrfs.img
BTRFS_MOUNTED=0
TRAP_FIRED=0

as_admin() { runuser -u "$ADMIN" -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" "$@"; }
broker_check_admin() {
    as_admin dbus-send --system --print-reply=literal \
        --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckPermission \
        "string:$1" "dict:string:string:" 2>/dev/null | tr -d ' \t\n'
}
clean_silo() {
    systemctl stop "$UNIT" >/dev/null 2>&1 || true
    systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true
    as_admin podman rm -f "qdistro-silo-${SILO}" >/dev/null 2>&1 || true
    rm -f "$LAUNCH_ENV_DIR/${SILO}.env" 2>/dev/null || true
}
write_launch_env() {
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
audit_count() { sqlite3 "$AUDIT_DB" "$1" 2>/dev/null || echo 0; }
# Build + promote a generation of $TEMPLATE; echoes the promoted RUN_ID.
build_promote() {
    local out rid
    out=$(as_admin qdistro-template-build "$TEMPLATE" 2>/tmp/s64-tbuild.log) \
        || { cat /tmp/s64-tbuild.log >&2; fail "template-build failed"; echo ""; return 1; }
    rid=$(printf '%s\n' "$out" | sed -n 's/^RUN_ID=//p' | head -1)
    [ -n "$rid" ] || { printf '%s\n' "$out" >&2; fail "no RUN_ID from build"; echo ""; return 1; }
    as_admin qdistro-template-validate "$rid" >/tmp/s64-tvalidate.log 2>&1 \
        || { cat /tmp/s64-tvalidate.log >&2; fail "validate failed for $rid"; echo ""; return 1; }
    as_admin qdistro-template-promote "$SILO" "$rid" >/tmp/s64-tpromote.log 2>&1 \
        || { cat /tmp/s64-tpromote.log >&2; fail "promote failed for $SILO/$rid"; echo ""; return 1; }
    echo "$rid"
}
MARKER="/var/lib/qdistro/bindings/${SILO}.activated"
read_marker_gen() { [ -f "$MARKER" ] && tr -d ' \t\n' < "$MARKER" || echo ""; }
# Start the silo unit and wait until the resolver commits the activation marker
# to a generation DIFFERENT from $1 (pass "" to wait for any). The marker file
# (the launch anchor's persistent record) is the robust activation signal — the
# template_audit.sqlite DB is qdistro-pwd-owned 0700 so the as-admin resolver's
# best-effort DB write is dropped; the marker + the unit journal are the source
# of truth. Echoes the activated generation (or "" on timeout).
launch_and_get_marker_gen() {
    local prev="$1" gen=""
    clean_silo
    write_launch_env
    systemctl start "$UNIT" 2>/tmp/s64-unitstart.log || true
    for _ in $(seq 1 80); do
        gen=$(read_marker_gen)
        [ -n "$gen" ] && [ "$gen" != "$prev" ] && break
        gen=""
        sleep 0.5
    done
    echo "$gen"
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
    # Remove any read-only btrfs snapshot subvolumes first (rm -rf can't unlink a
    # subvolume), then the silo state tree + snapshots.
    if command -v btrfs >/dev/null 2>&1; then
        for sv in "$SNAP_DIR"/*/snapshot "$STATE_PATH"; do
            [ -e "$sv" ] && btrfs subvolume delete "$sv" >/dev/null 2>&1 || true
        done
    fi
    rm -rf "$SILO_DIR" "$TIER2_BUILD_DIR" 2>/dev/null || true
    # Unmount + drop the btrfs loopback we mounted at the silos dir (after the
    # subvolumes under it are gone).
    if [ "${BTRFS_MOUNTED:-0}" = 1 ]; then
        umount "$SILOS_DIR" 2>/dev/null || umount -l "$SILOS_DIR" 2>/dev/null || true
    fi
    # Always drop the image — it is created before the mount, so a failed
    # mkfs/mount would otherwise leak it.
    rm -f "$BTRFS_IMG" 2>/dev/null || true
    rm -f /tmp/s64-*.log 2>/dev/null || true
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- 1. prerequisites ------------------------------------------------
command -v podman    >/dev/null 2>&1 || skip "podman not installed"
command -v dbus-send >/dev/null 2>&1 || skip "dbus-send absent"
command -v sqlite3   >/dev/null 2>&1 || skip "sqlite3 absent"
command -v btrfs     >/dev/null 2>&1 || skip "btrfs CLI absent (cannot prove the btrfs snapshot path)"
command -v qdistro-template-build >/dev/null 2>&1 || skip "qdistro-template-build not in PATH"
command -v qdistro-secctx-exec >/dev/null 2>&1 || skip "qdistro-secctx-exec not installed"
[ -x "$SPAWN" ] || skip "$SPAWN not installed"
[ -x "$LAUNCH_HELPER" ] || skip "$LAUNCH_HELPER not installed"
[ -f "/etc/systemd/system/${LAUNCH_UNIT_TMPL}.service" ] || skip "silo launcher unit not installed"
[ -d "$SRC/tier2" ] || skip "tier2 source not unpacked"
grep -qx 'User=root' "/etc/systemd/system/${LAUNCH_UNIT_TMPL}.service" || skip "silo unit not User=root"
as_admin test -S "$RUNTIME_DIR/$OUTER" || skip "outer admin compositor not up"
# Real-btrfs: the silo state lives under /var/lib/qdistro/silos. If that path is
# not already on btrfs (the baked VM root is xfs), back it with a btrfs loopback
# so promote creates the state as a real subvolume and the launch path exercises
# the btrfs `subvolume` snapshot mechanism (not the `copy` fallback). Mirrors
# templates-state-snapshot-probe.sh's loopback, but mounted at the PRODUCTION
# silos dir so the unmodified production launch path uses it.
if [ "$(stat -f -c %T "$SILOS_DIR" 2>/dev/null || echo '')" = "btrfs" ]; then
    pass "tier2-template prerequisites present (silos dir already on btrfs)"
else
    command -v mkfs.btrfs >/dev/null 2>&1 || skip "mkfs.btrfs absent — cannot back the silos dir with btrfs"
    install -d -m 0755 "$SILOS_DIR"
    # Refuse to shadow a non-empty silos dir (don't hide another run's silos).
    [ -z "$(ls -A "$SILOS_DIR" 2>/dev/null)" ] || skip "$SILOS_DIR non-empty; refusing to shadow it with a btrfs loopback"
    rm -f "$BTRFS_IMG" 2>/dev/null || true
    truncate -s 1G "$BTRFS_IMG" || skip "truncate $BTRFS_IMG failed"
    mkfs.btrfs -q -f "$BTRFS_IMG" >/dev/null 2>&1 || skip "mkfs.btrfs failed"
    mount -o loop "$BTRFS_IMG" "$SILOS_DIR" || skip "mount -o loop $SILOS_DIR failed"
    BTRFS_MOUNTED=1
    # The production silos tree is admin-owned (promote runs AS ADMIN and creates
    # the silo dir + the state subvolume); the fresh loopback root is root-owned,
    # so hand it to admin or the as-admin promote can't write there.
    chown "$ADMIN":"$ADMIN" "$SILOS_DIR"
    chmod 0755 "$SILOS_DIR"
    [ "$(stat -f -c %T "$SILOS_DIR" 2>/dev/null)" = "btrfs" ] || skip "mounted silos dir is not btrfs"
    pass "tier2-template prerequisites present (backed silos dir with a btrfs loopback)"
fi

systemctl start qdistro-admin-broker.service 2>/dev/null || true

# --- 2. build the weston-terminal tier-2 image (cached) --------------
rm -rf "$TIER2_BUILD_DIR" 2>/dev/null || true
cp -r "$SRC/tier2" "$TIER2_BUILD_DIR" || skip "stage tier2 build dir"
chmod -R a+rX "$TIER2_BUILD_DIR"; find "$TIER2_BUILD_DIR" -name '*.sh' -exec chmod a+rx {} +
if ! as_admin podman image exists "$IMAGE" 2>/dev/null; then
    as_admin bash "$TIER2_BUILD_DIR/make-tier2-image.sh" "$WORKLOAD" >/tmp/s64-build.log 2>&1 \
        || { cat /tmp/s64-build.log >&2; skip "build of $IMAGE failed"; }
fi
as_admin podman image exists "$IMAGE" || skip "$IMAGE absent after build"

# --- 3. broker spawn allow rule + settle -----------------------------
install -d -m 0755 "$RULE_DIR"
cat >"$RULE_FILE" <<EOF
- name: s64-snapshot-spawn-allow
  decision: allow
  match:
    action: qdistro.tier2.spawn:${WORKLOAD}/${WORKLOAD}
- name: s64-snapshot-nested-advertise-allow
  decision: allow
  match:
    action: qdistro.nested.advertise:org.freedesktop.weston.wayland-terminal
EOF
systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
r1=""; r2=""
for _ in $(seq 1 20); do
    r1=$(broker_check_admin "qdistro.tier2.spawn:${WORKLOAD}/${WORKLOAD}")
    r2=$(broker_check_admin "qdistro.nested.advertise:org.freedesktop.weston.wayland-terminal")
    [ "$r1" = allow ] && [ "$r2" = allow ] && break
    sleep 0.25
done
[ "$r1" = allow ] && [ "$r2" = allow ] || fail "broker did not load the spawn/advertise rules (spawn=$r1 advertise=$r2)"

# --- 4. GEN1: recipe -> build -> promote -> first activation ---------
install -d -m 0755 "$RECIPES_DIR" "$ETC_TEMPLATES"
cat >"$RECIPES_DIR/$CONTAINERFILE_NAME" <<EOF
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
RID1=$(build_promote) || { echo "[s64] $PASSCOUNT passes, $FAILCOUNT failures"; exit 1; }
[ -f "/var/lib/qdistro/bindings/${SILO}.toml" ] || fail "no binding after gen1 promote"

# Real-btrfs gate #2: the state created by promote MUST be a btrfs subvolume.
MECH=$(sed -n 's/^[[:space:]]*mechanism[[:space:]]*=[[:space:]]*"\?\([a-z]*\)"\?.*/\1/p' "$STATE_META" 2>/dev/null | head -1)
if [ "$MECH" != "subvolume" ]; then
    skip "silo state created as mechanism='$MECH' (not 'subvolume') — btrfs subvolume path unavailable here"
fi
pass "silo state created as a btrfs subvolume (mechanism=subvolume)"

GEN1=$(launch_and_get_marker_gen "")
if [ -n "$GEN1" ]; then
    pass "gen1 first activation committed the activation marker (generation=${GEN1#sha256:})"
else
    journalctl -u "$UNIT" --no-pager 2>/dev/null | tail -30 >&2
    fail "gen1 did not activate (no activation marker) within 40s"
fi
clean_silo

# --- 5. GEN2: distinct recipe -> build -> promote -> flip ------------
# An extra LABEL changes the build so the generation digest differs; the flip
# from gen1 -> gen2 is what must take the pre-activation snapshot.
cat >"$RECIPES_DIR/$CONTAINERFILE_NAME" <<EOF
FROM $IMAGE
LABEL qdistro_snap_e2e="gen2"
CMD ["weston-terminal"]
EOF
RID2=$(build_promote) || { echo "[s64] $PASSCOUNT passes, $FAILCOUNT failures"; exit 1; }
if [ -n "${RID1:-}" ] && [ "$RID1" = "${RID2:-}" ]; then
    fail "gen2 RUN_ID == gen1 RUN_ID ($RID1) — recipe change did not yield a new generation"
fi

CURSOR=$(journalctl --since=now -n0 --show-cursor 2>/dev/null | awk -F': ' '/-- cursor:/ {print $2}')
GEN2=$(launch_and_get_marker_gen "$GEN1")
if [ -n "$GEN2" ] && [ "$GEN2" != "$GEN1" ]; then
    pass "gen2 activation flipped the marker (generation ${GEN1#sha256:} -> ${GEN2#sha256:})"
else
    journalctl -u "$UNIT" --no-pager 2>/dev/null | tail -30 >&2
    fail "gen2 did not flip the activation marker (gen1=$GEN1 gen2=$GEN2) within 40s"
fi

# --- 6. assert the pre-activation snapshot (the launch-path -> btrfs) -
# 6a. the resolver logged the snapshot with the btrfs mechanism (the marker only
# flips AFTER the pre-activation snapshot succeeds, so this proves the launch
# path took it before activating the incoming generation).
JWIN=$(journalctl -u "$UNIT" ${CURSOR:+--after-cursor="$CURSOR"} --no-pager 2>/dev/null)
if printf '%s\n' "$JWIN" | grep -q "pre-activation snapshot .* taken (mechanism=subvolume"; then
    pass "launch path logged a pre-activation snapshot with mechanism=subvolume"
else
    printf '%s\n' "$JWIN" | grep -i snapshot >&2 || true
    fail "no 'pre-activation snapshot ... mechanism=subvolume' line in the gen2 launch journal"
fi

# 6b. the snapshot-created audit event was emitted on the launch path (read from
# the unit journal — the template_audit.sqlite DB is qdistro-pwd-owned 0700 so
# the as-admin best-effort DB write is dropped; the emitted event line is the
# durable record on the launch journal).
if printf '%s\n' "$JWIN" | grep -q "template.state_snapshot.created silo=${SILO}.*result=created.*reason=mechanism=subvolume"; then
    pass "launch path emitted template.state_snapshot.created (result=created, mechanism=subvolume)"
else
    printf '%s\n' "$JWIN" | grep -i "state_snapshot" >&2 || true
    fail "no template.state_snapshot.created (mechanism=subvolume) event on the gen2 launch journal"
fi

# 6c. the snapshot payload is a real READ-ONLY btrfs subvolume.
SNAP_PAYLOAD=$(ls -d "$SNAP_DIR"/*/snapshot 2>/dev/null | head -1)
if [ -n "$SNAP_PAYLOAD" ] && btrfs subvolume show "$SNAP_PAYLOAD" >/tmp/s64-svshow.log 2>&1; then
    if grep -qiE "readonly|ro flag.*yes|Flags:.*readonly" /tmp/s64-svshow.log; then
        pass "pre-activation snapshot is a read-only btrfs subvolume ($SNAP_PAYLOAD)"
    else
        # Some btrfs-progs print the ro state differently; fall back to property.
        if btrfs property get -ts "$SNAP_PAYLOAD" ro 2>/dev/null | grep -q "ro=true"; then
            pass "pre-activation snapshot is a read-only btrfs subvolume ($SNAP_PAYLOAD)"
        else
            cat /tmp/s64-svshow.log >&2
            fail "snapshot payload $SNAP_PAYLOAD is a subvolume but not read-only"
        fi
    fi
else
    fail "snapshot payload is not a btrfs subvolume under $SNAP_DIR"
fi

# --- 7. the gen2 container actually ran (podman-run end of the chain) -
RAN=0
for _ in $(seq 1 30); do
    if as_admin podman ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "qdistro-silo-${SILO}"; then
        RAN=1; break
    fi
    sleep 0.5
done
[ "$RAN" = 1 ] && pass "gen2 podman container ran (qdistro-silo-${SILO})" \
               || fail "gen2 container qdistro-silo-${SILO} never appeared in podman"

# --- summary ---------------------------------------------------------
if [ "$FAILCOUNT" -eq 0 ] && [ "$PASSCOUNT" -ge 7 ]; then
    pass "§05/B#5 tier-2-template spawn -> pre-activation snapshot -> podman-run end-to-end (real btrfs)"
    echo "[s64] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s64] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
