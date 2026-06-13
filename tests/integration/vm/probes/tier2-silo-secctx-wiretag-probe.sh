#!/bin/bash
# tier2-silo-secctx-wiretag-probe — prove a PERSISTENT templated tier-2 silo's
# secctx app_id (qdistro-silo-<name>/<app>) reaches the outer compositor ON THE
# WIRE via wp_security_context_v1, through the PRODUCTION launch path: the
# qdistro-tier2-silo@<name>.service unit (now User=root) -> qdistro-tier2-silo-
# launch -> spawn-tier2 in root-launcher mode (TIER2_ROOT_LAUNCHER=1). This is
# the silo analogue of disp-secctx-wiretag-probe.sh and closes the residual the
# qdistro-tier2-silo@.service comment flagged: the old User=admin unit ran the
# silo UN-TAGGED (no direct root launcher parent), tagged only under the dev
# override (QDWIN_SECCTX_OPEN=1 + QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED=1).
#
# We drive a REAL, REAL-binding-resolved templated silo end-to-end (the daemon
# only ever launches templated silos — the unit's helper hard-requires
# TIER2_SILO), so the proof is faithful, NOT a binding-less shortcut. The silo's
# generation is built from the shipped weston-terminal tier-2 image (a real
# wayland client that triggers the secctx commit) via a tiny FROM-only recipe,
# then promoted into the admin-owned PRODUCTION tree. The single launch proves:
#
#  (wiretag) qdwin's OWN commit-handler journal line carries the silo's secctx
#    triple ON THE WIRE — with NO dev override (acceptance rests on the
#    production root-parent attestation). The container runs in admin's ROOTLESS
#    podman (NOT root's store). The root-unit ExecStop (now admin-dropped) tears
#    the admin container down.
#
#  (resolver-admin) The binding resolver + its activation marker / run-status
#    files were written AS ADMIN (not root) — the fix that root-launcher mode
#    drops the resolver to admin via `as_admin_run`, preserving the admin-owned
#    binding tree even though the unit is now User=root.
#
#  (fail-closed) The launch helper EXITS NONZERO (no un-tagged silo) when it
#    cannot resolve a non-root admin uid; the stop helper refuses uid 0; and
#    spawn-tier2's root-launcher gate refuses when it cannot stamp the tag.
#
# Runs as root INSIDE the test VM (staged to /root). Every PASS line is
# asserted by tier2-silo-secctx-wiretag.bats.
set -u

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
RULE_FILE="$RULE_DIR/zz-silo-wiretag-allow.yaml"
TIER2_BUILD_DIR=/tmp/qd-silo-wiretag-tier2
LAUNCH_ENV_DIR=/run/qdistro/silo-launch
RECIPES_DIR=/usr/lib/qdistro/templates/recipes
ETC_TEMPLATES=/etc/qdistro/templates

# The silo + its template. The recipe is a 1-line FROM the shipped
# weston-terminal image (near-instant build, a real wayland client to commit
# secctx). Safe-name: [a-z_][a-z0-9_-]*.
SILO=silowt
TEMPLATE=silowt
CONTAINERFILE_NAME=Containerfile.silowt

APPID_RE='^qdistro-silo-[a-z0-9_-]+/[A-Za-z0-9._-]+$'
TOKEN_RE='^[0-9a-f]{32}$'

fail() { printf 'FAIL: %s — %s\n' "$1" "${2:-}" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$1"; }
as_admin() { runuser -u "$ADMIN" -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" "$@"; }

broker_check_admin() {
    as_admin dbus-send --system --print-reply=literal \
        --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckPermission \
        "string:$1" "dict:string:string:" 2>/dev/null | tr -d ' \t\n'
}

write_launch_env() {
    # Mirror the daemon's _export_tier2_launch_env exactly (single-quoted
    # KEY='VALUE'). The daemon ALWAYS sets a non-empty template_silo for a
    # tier2-template silo, so TIER2_SILO == the silo name (binding-resolved).
    # The admin identity is fixed to `admin`; this env file carries only silo
    # launch metadata.
    #
    # SECURITY: the launch + stop helpers `.`-source this file AS ROOT, so it
    # must be root-owned and non-group/other-writable or they refuse it. The
    # daemon writes it root-owned 0600; mirror that here (the env-file dir is
    # tmpfs /run, the file 0600 root:root).
    install -d -m 0755 "$LAUNCH_ENV_DIR"
    local f="$LAUNCH_ENV_DIR/${SILO}.env"
    cat >"$f" <<EOF
TIER2_SILO='${SILO}'
TIER2_NETWORK='none'
QD_WORKLOAD='${WORKLOAD}'
QD_CONTAINER='qdistro-silo-${SILO}'
QD_APP_ARGV_JSON='["${WORKLOAD}"]'
EOF
    chown 0:0 "$f"
    chmod 0600 "$f"
}

clean_silo() {
    systemctl stop "${LAUNCH_UNIT_TMPL}${SILO}.service" >/dev/null 2>&1 || true
    systemctl reset-failed "${LAUNCH_UNIT_TMPL}${SILO}.service" >/dev/null 2>&1 || true
    as_admin podman rm -f "qdistro-silo-${SILO}" >/dev/null 2>&1 || true
    rm -f "$LAUNCH_ENV_DIR/${SILO}.env" 2>/dev/null || true
}

cmd_setup() {
    command -v podman    >/dev/null 2>&1 || fail setup "podman not installed"
    command -v dbus-send >/dev/null 2>&1 || fail setup "dbus-send absent"
    command -v runuser   >/dev/null 2>&1 || fail setup "runuser absent"
    [ -x "$SPAWN" ]         || fail setup "$SPAWN not installed — PACKAGING GAP"
    [ -x "$LAUNCH_HELPER" ] || fail setup "$LAUNCH_HELPER not installed — PACKAGING GAP"
    [ -x "$STOP_HELPER" ]   || fail setup "$STOP_HELPER not installed — PACKAGING GAP (root-unit ExecStop drop)"
    [ -f "/etc/systemd/system/${LAUNCH_UNIT_TMPL}.service" ] \
        || fail setup "silo launcher unit not installed — PACKAGING GAP"
    command -v qdistro-secctx-exec >/dev/null 2>&1 \
        || fail setup "qdistro-secctx-exec not in PATH — wire-tag launcher cannot run (PACKAGING GAP)"
    command -v qdistro-template-build >/dev/null 2>&1 \
        || fail setup "qdistro-template-build not in PATH — cannot promote a real silo"

    # The unit MUST be User=root (the production wire-tag topology). A stale
    # User=admin unit would silently run un-tagged.
    grep -qx 'User=root' "/etc/systemd/system/${LAUNCH_UNIT_TMPL}.service" \
        || fail setup "silo unit is not User=root — the deployed unit predates the root-launcher wiring"

    as_admin test -S "$RUNTIME_DIR/$OUTER" \
        || fail setup "outer admin compositor not up ($RUNTIME_DIR/$OUTER missing)"

    systemctl start qdistro-admin-broker.service 2>/dev/null || true

    # Build the weston-terminal tier-2 image as ADMIN (rootless store) — it is
    # both our recipe's FROM base and the runtime the silo executes.
    rm -rf "$TIER2_BUILD_DIR" 2>/dev/null || true
    cp -r "$SRC/tier2" "$TIER2_BUILD_DIR" || fail setup "stage tier2 build dir"
    chmod -R a+rX "$TIER2_BUILD_DIR"
    find "$TIER2_BUILD_DIR" -name '*.sh' -exec chmod a+rx {} +
    if ! as_admin podman image exists "$IMAGE" 2>/dev/null; then
        as_admin bash "$TIER2_BUILD_DIR/make-tier2-image.sh" "$WORKLOAD" \
            >/tmp/silo-wiretag-build.log 2>&1 \
            || { cat /tmp/silo-wiretag-build.log >&2; fail setup "build of $IMAGE failed"; }
    fi
    as_admin podman image exists "$IMAGE" || fail setup "$IMAGE not present after build"

    # Drop a tiny FROM-only recipe + a minimal derived policy. The build just
    # re-tags the weston-terminal image (a real wayland client) under a
    # promoted generation digest — no package install, near-instant — so the
    # silo is genuinely binding-resolved (the only launch shape the daemon
    # emits) while still committing a real secctx triple to qdwin.
    install -d -m 0755 "$RECIPES_DIR" "$ETC_TEMPLATES"
    cat >"$RECIPES_DIR/$CONTAINERFILE_NAME" <<EOF
# Test-only recipe for the tier2-silo secctx wire-tag lane: FROM the shipped
# weston-terminal tier-2 image so the promoted generation IS a real wayland
# client (so the silo commits a secctx triple to qdwin), with no package work.
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

    # Broker: allow the SILO spawn gate for our app. The silo path uses the
    # qdistro.tier2.spawn:<workload>/<app> action; the gate now runs AS ADMIN
    # under root-launcher (so uid-scoped rules still match) — assert it loads.
    install -d -m 0755 "$RULE_DIR"
    cat >"$RULE_FILE" <<EOF
# Test-authored: allow the tier-2 silo spawn of $WORKLOAD so the silo secctx
# wire-tag lane can drive the root-launcher path. tier2-silo-secctx-wiretag.
- name: silo-wiretag-${WORKLOAD}-allow
  decision: allow
  match:
    action: qdistro.tier2.spawn:${WORKLOAD}/${WORKLOAD}
EOF
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    local reply=""
    for _ in $(seq 1 20); do
        reply=$(broker_check_admin "qdistro.tier2.spawn:${WORKLOAD}/${WORKLOAD}")
        [ "$reply" = "allow" ] && break
        sleep 0.25
    done
    [ "$reply" = "allow" ] \
        || fail setup "broker did not load the silo allow rule (CheckPermission='$reply')"

    # Build + validate + promote the silo into the admin-owned PRODUCTION tree
    # (default Layout, /var/lib/qdistro) AS ADMIN — that is the identity that
    # must own the binding tree.
    local build_out rid
    build_out=$(as_admin qdistro-template-build "$TEMPLATE" 2>/tmp/silo-tbuild.log) \
        || { cat /tmp/silo-tbuild.log >&2; fail setup "template-build failed"; }
    rid=$(printf '%s\n' "$build_out" | sed -n 's/^RUN_ID=//p' | head -1)
    [ -n "$rid" ] || { printf '%s\n' "$build_out" >&2; fail setup "no RUN_ID from build"; }
    as_admin qdistro-template-validate "$rid" >/tmp/silo-tvalidate.log 2>&1 \
        || { cat /tmp/silo-tvalidate.log >&2; fail setup "validate failed for $rid"; }
    as_admin qdistro-template-promote "$SILO" "$rid" >/tmp/silo-tpromote.log 2>&1 \
        || { cat /tmp/silo-tpromote.log >&2; fail setup "promote failed for $SILO/$rid"; }

    local binding="/var/lib/qdistro/bindings/${SILO}.toml"
    [ -f "$binding" ] || fail setup "no binding at $binding after promote"
    [ "$(stat -c '%U' "$binding")" = "$ADMIN" ] \
        || fail setup "binding $binding not admin-owned after promote"

    clean_silo
    pass setup
}

cmd_wiretag() {
    clean_silo
    local unit="${LAUNCH_UNIT_TMPL}${SILO}.service"
    local container="qdistro-silo-${SILO}"

    # Clear prior resolver side-effects so we can attribute the ones THIS launch
    # writes (ownership is the resolver-admin assertion below).
    rm -f "/run/qdistro/silo-generation/${SILO}" \
          "/var/lib/qdistro/bindings/${SILO}.activated" 2>/dev/null || true

    write_launch_env

    local cursor
    cursor=$(journalctl --since=now -n0 --show-cursor 2>/dev/null \
             | awk -F': ' '/-- cursor:/ {print $2}')
    [ -n "$cursor" ] || fail wiretag "could not capture a journal cursor"

    # START THE REAL UNIT (root) -> qdistro-tier2-silo-launch resolves the admin
    # uid and execs spawn-tier2 with TIER2_ROOT_LAUNCHER=1.
    systemctl start "$unit" \
        || { journalctl -u "$unit" --after-cursor="$cursor" | tail -30 >&2; \
             fail wiretag "systemctl start $unit failed"; }

    # ---- the helper computed a SILO app_id (from the unit's journal) --------
    local appid="" token="" deadline=$(( $(date +%s) + 40 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        appid=$(journalctl -u "$unit" --after-cursor="$cursor" 2>/dev/null \
                | sed -n 's/.*APP_ID=\(qdistro-silo-[^ ]*\).*/\1/p' | head -1)
        token=$(journalctl -u "$unit" --after-cursor="$cursor" 2>/dev/null \
                | sed -n 's/.*LAUNCH_TOKEN=\([0-9a-f]\{32\}\).*/\1/p' | head -1)
        [ -n "$appid" ] && [ -n "$token" ] && break
        sleep 0.5
    done
    [ -n "$appid" ] && [ -n "$token" ] \
        || { journalctl -u "$unit" --after-cursor="$cursor" | tail -40 >&2; \
             fail wiretag "unit emitted no SILO APP_ID/LAUNCH_TOKEN"; }
    [[ "$appid" =~ $APPID_RE ]] \
        || fail wiretag "app_id '$appid' is not a qdistro-silo-<name>/<app> id"
    [[ "$token" =~ $TOKEN_RE ]] \
        || fail wiretag "launch token '$token' is not 32 hex"
    [ "$appid" = "qdistro-silo-${SILO}/${WORKLOAD}" ] \
        || fail wiretag "app_id '$appid' is not the expected silo id qdistro-silo-${SILO}/${WORKLOAD}"
    pass "silo unit computed the silo secctx app_id ($appid) + instance ($token)"

    # Guard: the root-launcher path must NOT have downgraded to un-tagged.
    if journalctl -u "$unit" --after-cursor="$cursor" 2>/dev/null | grep -q 'running un-tagged'; then
        journalctl -u "$unit" --after-cursor="$cursor" | tail -30 >&2
        fail wiretag "silo unit ran UN-TAGGED (it should have stamped the wire tag)"
    fi
    pass "silo path did not downgrade to the un-tagged fallback"

    # ---- resolver-admin: the binding resolver ran AS ADMIN -----------------
    # This is the fix root-launcher mode introduces for silos: the resolver
    # (--record, which writes the per-boot run-status + the activation marker
    # into the admin-owned 0700 binding tree) must run as admin, not root.
    local saw_status=""
    if [ -e "/run/qdistro/silo-generation/${SILO}" ]; then
        saw_status=1
        local sown; sown=$(stat -c '%U' "/run/qdistro/silo-generation/${SILO}")
        [ "$sown" = "$ADMIN" ] \
            || fail wiretag "run-status /run/qdistro/silo-generation/${SILO} owned by '$sown', not admin — the resolver ran as ROOT (drop missing)"
    fi
    if [ -e "/var/lib/qdistro/bindings/${SILO}.activated" ]; then
        saw_status=1
        local mown; mown=$(stat -c '%U' "/var/lib/qdistro/bindings/${SILO}.activated")
        [ "$mown" = "$ADMIN" ] \
            || fail wiretag "activation marker for ${SILO} owned by '$mown', not admin — root-owned file in the admin-owned 0700 binding tree (resolver did not drop)"
    fi
    [ -n "$saw_status" ] \
        || fail wiretag "binding resolver wrote no run-status/marker — did it run? (it should have, the silo is binding-resolved)"
    pass "binding resolver ran AS ADMIN (run-status/marker admin-owned; root-launcher dropped the resolver)"

    # ---- THE WIRE TAG: qdwin's secctx commit logged the SILO app_id ---------
    local commit="" cdl=$(( $(date +%s) + 40 ))
    while [ "$(date +%s)" -lt "$cdl" ]; do
        commit=$(journalctl --after-cursor="$cursor" 2>/dev/null \
            | grep -m1 -E "qdwin/secctx: committed engine=qdistro\.tier2 app_id=${appid//\//\\/} instance_id=${token}" \
            || true)
        [ -n "$commit" ] && break
        sleep 0.5
    done
    if [ -z "$commit" ]; then
        echo "--- unit journal ---" >&2
        journalctl -u "$unit" --after-cursor="$cursor" | tail -30 >&2
        echo "--- recent qdwin secctx journal ---" >&2
        journalctl --after-cursor="$cursor" 2>/dev/null | grep -i 'secctx' | tail -20 >&2
        fail wiretag "qdwin never logged the silo secctx commit (app_id=$appid instance_id=$token) — the silo app_id did NOT reach the compositor on the wire"
    fi
    echo "$commit" >&2
    pass "qdwin received the SILO secctx app_id ON THE WIRE (committed engine=qdistro.tier2 app_id=$appid instance_id=$token)"

    # ---- no rootful-podman confusion ---------------------------------------
    as_admin podman container exists "$container" 2>/dev/null \
        || { journalctl -u "$unit" --after-cursor="$cursor" | tail -20 >&2; \
             fail wiretag "container '$container' not in admin's rootless podman (did the run drop to admin?)"; }
    pass "silo container lives in admin's rootless podman (no rootful run)"
    if podman container exists "$container" 2>/dev/null; then
        fail wiretag "container '$container' ALSO exists in ROOT's podman store — rootful confusion"
    fi
    pass "silo container absent from root's podman store (rootless drop confirmed)"

    # ---- ExecStop (now admin-dropped) tears the admin container down --------
    systemctl stop "$unit" >/dev/null 2>&1 || true
    local gone="" gdl=$(( $(date +%s) + 40 ))
    while [ "$(date +%s)" -lt "$gdl" ]; do
        if ! as_admin podman container exists "$container" 2>/dev/null; then gone=1; break; fi
        sleep 0.5
    done
    [ -n "$gone" ] \
        || fail wiretag "silo container '$container' survived 'systemctl stop' — the root-unit ExecStop did not drop to admin and orphaned it"
    pass "systemctl stop -> admin container gone (root-unit ExecStop dropped to admin)"

    systemctl reset-failed "$unit" >/dev/null 2>&1 || true
    pass wiretag
}

# ---- (fail-closed) the helper must refuse, never launch un-tagged ----------
cmd_fail_closed() {
    clean_silo
    local out err rc

    # (a) launch helper with a non-existent admin user (driven THROUGH the env
    #     file — the source of truth — exactly as a real non-default-admin
    #     deployment would) must EXIT NONZERO + mint nothing.
    write_launch_env nosuchadmin
    out=$(mktemp); err=$(mktemp)
    "$LAUNCH_HELPER" "$SILO" >"$out" 2>"$err"
    rc=$?
    [ "$rc" -ne 0 ] \
        || { echo "--- stdout ---" >&2; cat "$out" >&2; \
             fail fail-closed "helper SUCCEEDED with a missing admin user (rc=0) — fail-open"; }
    grep -q 'does not exist' "$err" \
        || { cat "$err" >&2; fail fail-closed "helper did not fail with the expected missing-admin refusal"; }
    grep -q 'LAUNCH_TOKEN=' "$out" \
        && fail fail-closed "helper emitted a LAUNCH_TOKEN despite the refusal — it spawned"
    rm -f "$out" "$err"
    pass "launch helper refuses a missing admin user FROM THE ENV FILE (no un-tagged spawn)"

    # (b) the stop helper must refuse uid 0 (would orphan the admin container).
    #     Again driven through the env file so the env-file resolution path is
    #     what refuses (root resolves to uid 0).
    write_launch_env root
    out=$(mktemp); err=$(mktemp)
    "$STOP_HELPER" "$SILO" >"$out" 2>"$err"
    rc=$?
    [ "$rc" -ne 0 ] \
        || fail fail-closed "stop helper SUCCEEDED for uid-0 admin (would 'stop' root's empty store, orphaning the admin container)"
    grep -q 'uid 0' "$err" \
        || { cat "$err" >&2; fail fail-closed "stop helper did not refuse uid 0 with the expected message"; }
    rm -f "$out" "$err"
    pass "stop helper refuses an admin user (from env file) that resolves to uid 0"

    # (b2) SECURITY: a launch env file NOT owned by root (admin-writable) must be
    #      REFUSED, never `.`-sourced as root. Plant an admin-owned env file and
    #      assert both helpers refuse it.
    write_launch_env
    chown "$ADMIN":"$ADMIN" "$LAUNCH_ENV_DIR/${SILO}.env" 2>/dev/null || true
    out=$(mktemp); err=$(mktemp)
    "$LAUNCH_HELPER" "$SILO" >"$out" 2>"$err"
    rc=$?
    [ "$rc" -ne 0 ] \
        || fail fail-closed "launch helper SOURCED an admin-owned env file as root (rc=0) — root TCB breach"
    grep -qi 'not root\|refusing to source' "$err" \
        || { cat "$err" >&2; fail fail-closed "launch helper did not refuse the non-root-owned env file"; }
    rm -f "$out" "$err"
    out=$(mktemp); err=$(mktemp)
    "$STOP_HELPER" "$SILO" >"$out" 2>"$err"
    rc=$?
    [ "$rc" -ne 0 ] \
        || fail fail-closed "stop helper SOURCED an admin-owned env file as root (rc=0) — root TCB breach"
    grep -qi 'not\|refusing to source' "$err" \
        || { cat "$err" >&2; fail fail-closed "stop helper did not refuse the non-root-owned env file"; }
    rm -f "$out" "$err"
    # Restore a safe (root-owned) env file for any later steps.
    write_launch_env
    pass "launch + stop helpers REFUSE a non-root-owned (admin-writable) launch env (root TCB)"

    # (c) spawn-tier2 root-launcher gate still fails closed when it cannot stamp
    #     (TIER2_USE_SECCTX=0) — defence at the spawn layer too.
    out=$(mktemp); err=$(mktemp)
    timeout 30 env TIER2_ROOT_LAUNCHER=1 TIER2_ADMIN_UID="$ADMIN_UID" \
        TIER2_USE_SECCTX=0 WAYLAND_DISPLAY="$OUTER" \
        "$SPAWN" "qdistro-silo-${SILO}" "$WORKLOAD" -- "$WORKLOAD" >"$out" 2>"$err"
    rc=$?
    [ "$rc" -ne 0 ] \
        || { echo "--- stdout ---" >&2; cat "$out" >&2; \
             fail fail-closed "spawn-tier2 root-launcher+USE_SECCTX=0 SUCCEEDED (rc=0) — fail-open un-tagged"; }
    grep -q 'requires TIER2_USE_SECCTX=1' "$err" \
        || { cat "$err" >&2; fail fail-closed "spawn-tier2 did not refuse USE_SECCTX=0 in root-launcher mode"; }
    rm -f "$out" "$err"
    pass "spawn-tier2 root-launcher refuses TIER2_USE_SECCTX=0 (no un-tagged silo)"

    clean_silo
    pass fail-closed
}

cmd_teardown() {
    clean_silo
    rm -f "$RULE_FILE" 2>/dev/null || true
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    # Best-effort: drop the promoted silo binding/state + the test template.
    rm -f "/var/lib/qdistro/bindings/${SILO}.toml" \
          "/var/lib/qdistro/bindings/${SILO}.activated" \
          "/run/qdistro/silo-generation/${SILO}" 2>/dev/null || true
    rm -rf "/var/lib/qdistro/silos/${SILO}" 2>/dev/null || true
    rm -f "$ETC_TEMPLATES/$TEMPLATE.toml" "$RECIPES_DIR/$CONTAINERFILE_NAME" 2>/dev/null || true
    rm -rf "$TIER2_BUILD_DIR" /tmp/silo-wiretag-build.log /tmp/silo-t*.log 2>/dev/null || true
    pass teardown
}

case "${1:-}" in
    setup) cmd_setup ;;
    wiretag) cmd_wiretag ;;
    fail-closed) cmd_fail_closed ;;
    teardown) cmd_teardown ;;
    *) echo "usage: $0 {setup|wiretag|fail-closed|teardown}" >&2; exit 2 ;;
esac
