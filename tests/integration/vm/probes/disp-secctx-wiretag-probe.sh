#!/bin/bash
# disp-secctx-wiretag-probe — prove a tier-2 DISPOSABLE's secctx app_id
# (qdistro.disp.<token>) actually reaches the outer compositor ON THE WIRE via
# wp_security_context_v1, through the PRODUCTION root-launcher path
# (TIER2_ROOT_LAUNCHER=1). This closes the residual the M3 disposables lane
# (disp-probe.sh) deferred to "phase7-secctx": that lane ran an ADMIN-driven
# spawn, which qdwin's hardened secctx authorization refuses to tag (the helper
# has no direct root launcher parent), so the disposable ran UN-TAGGED. Here we
# drive the same shipped /usr/bin/qdistro-tier2-spawn --disposable but as the
# root launcher, and assert qdwin LOGGED the commit of the disp app_id.
#
# The deterministic wire signal is qdwin's own commit-handler journal line
# (qdwin/qdwin.c:19356), exactly as the tier-3 lane s40-secctx.sh asserts —
# NOT a screenshot. The secctx triple a screenshot could never show
# (engine/app_id/instance_id) is precisely the thing on the wire, so we read it
# from the compositor's structured log:
#   qdwin/secctx: committed engine=qdistro.tier2 app_id=qdistro.disp.<token>
#                 instance_id=<launch-token> listen_fd=N close_fd=N
#
# Topology (mirrors tier3/spawn-tier3.sh:442-463): spawn-tier2 runs as ROOT and
# launches qdistro-secctx-exec via `runuser -u admin`, so the helper runs at
# the ADMIN uid (introspectable by the unprivileged admin compositor) while its
# DIRECT parent is `runuser` (root) — satisfying both secctx-exec's trusted-
# launcher check and qdwin's root-parent attestation. The inner app still runs
# in admin's ROOTLESS podman (--userns=keep-id), so we also assert the
# container exists ONLY in admin's podman, never root's store (no rootful
# confusion).
#
# Runs as root INSIDE the test VM (staged to /root). Every PASS line is
# asserted by disposable-secctx-wiretag.bats.
set -u

SRC=/root/qdistro-src/qdistro
SPAWN=/usr/bin/qdistro-tier2-spawn          # the SHIPPED artifact under test
ADMIN=admin
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER=wayland-1
WORKLOAD=weston-terminal                    # reuses the s32 tier-2 image
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
ADVERTISE_ACTION="qdistro.nested.advertise:org.freedesktop.weston.wayland-terminal"
RULE_DIR=/etc/qdistro/rules.d
RULE_FILE="$RULE_DIR/zz-disp-wiretag-${WORKLOAD}-allow.yaml"
TIER2_BUILD_DIR=/tmp/qd-disp-wiretag-tier2

APPID_RE='^qdistro\.disp\.[0-9a-f]{8,64}$'
TOKEN_RE='^[0-9a-f]{32}$'

fail() { printf 'FAIL: %s — %s\n' "$1" "${2:-}" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$1"; }
as_admin() { runuser -u "$ADMIN" -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" "$@"; }

broker_check() {
    as_admin dbus-send --system --print-reply=literal \
        --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckPermission \
        "string:$1" "dict:string:string:" 2>/dev/null | tr -d ' \t\n'
}

clean_disp() {
    local n
    for n in $(as_admin podman ps -a --filter label=qdistro_disposable=1 \
               --format '{{.Names}}' 2>/dev/null); do
        as_admin podman rm -f "$n" >/dev/null 2>&1 || true
    done
}

cmd_setup() {
    command -v podman   >/dev/null 2>&1 || fail setup "podman not installed in this VM"
    command -v dbus-send >/dev/null 2>&1 || fail setup "dbus-send absent"
    command -v runuser  >/dev/null 2>&1 || fail setup "runuser absent (root-launcher path needs it)"
    [ -x "$SPAWN" ] || fail setup "$SPAWN not installed — PACKAGING GAP"
    [ -f /usr/lib/qdistro/spawn-common.sh ] || fail setup "/usr/lib/qdistro/spawn-common.sh missing — PACKAGING GAP"
    command -v qdistro-secctx-exec >/dev/null 2>&1 \
        || fail setup "qdistro-secctx-exec not in PATH — the wire-tag launcher cannot run (PACKAGING GAP)"

    # Outer admin compositor (qdwin) up + its journal reachable — the wire tag
    # is a qdwin log line, so without qdwin there is nothing to assert.
    as_admin test -S "$RUNTIME_DIR/$OUTER" \
        || fail setup "outer admin compositor not up ($RUNTIME_DIR/$OUTER missing)"

    systemctl start qdistro-admin-broker.service 2>/dev/null || true

    # Build the tier-2 image as ADMIN (rootless store) — the root-launcher
    # spawn runs podman as admin, so the image must live in admin's store.
    rm -rf "$TIER2_BUILD_DIR" 2>/dev/null || true
    cp -r "$SRC/tier2" "$TIER2_BUILD_DIR" || fail setup "stage tier2 build dir"
    chmod -R a+rX "$TIER2_BUILD_DIR"
    find "$TIER2_BUILD_DIR" -name '*.sh' -exec chmod a+rx {} +
    if ! as_admin podman image exists "$IMAGE" 2>/dev/null; then
        as_admin bash "$TIER2_BUILD_DIR/make-tier2-image.sh" "$WORKLOAD" \
            >/tmp/disp-wiretag-build.log 2>&1 \
            || { cat /tmp/disp-wiretag-build.log >&2; fail setup "build of $IMAGE failed"; }
    fi
    as_admin podman image exists "$IMAGE" || fail setup "$IMAGE not present after build"

    install -d -m 0755 "$RULE_DIR"
    cat >"$RULE_FILE" <<EOF
# Test-authored: allow the disposable spawn of $WORKLOAD so the secctx
# wire-tag lane can drive the root-launcher path. disp-secctx-wiretag-probe.sh
- name: disp-wiretag-${WORKLOAD}-allow
  decision: allow
  match:
    action: qdistro.dispose.spawn:${WORKLOAD}
- name: disp-wiretag-${WORKLOAD}-nested-advertise-allow
  decision: allow
  match:
    action: ${ADVERTISE_ACTION}
EOF
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    local reply="" advertise_reply=""
    for _ in $(seq 1 20); do
        reply=$(broker_check "qdistro.dispose.spawn:${WORKLOAD}")
        advertise_reply=$(broker_check "$ADVERTISE_ACTION")
        [ "$reply" = "allow" ] && [ "$advertise_reply" = "allow" ] && break
        sleep 0.25
    done
    [ "$reply" = "allow" ] && [ "$advertise_reply" = "allow" ] \
        || fail setup "broker did not load the disp allow rules (spawn='$reply' nested-advertise='$advertise_reply')"

    clean_disp
    pass setup
}

cmd_wiretag() {
    clean_disp
    out=$(mktemp); err=$(mktemp)
    container=""; SPAWN_PID=""
    # shellcheck disable=SC2317
    _wt_cleanup() {
        [ -n "${container:-}" ] && as_admin podman rm -f "$container" >/dev/null 2>&1
        [ -n "${SPAWN_PID:-}" ] && kill "$SPAWN_PID" 2>/dev/null
        rm -f "$out" "$err" 2>/dev/null
        return 0
    }
    trap _wt_cleanup EXIT

    # Journal cursor so the commit assertion only sees lines from THIS launch.
    local cursor
    cursor=$(journalctl --since=now -n0 --show-cursor 2>/dev/null \
             | awk -F': ' '/-- cursor:/ {print $2}')
    [ -n "$cursor" ] || fail wiretag "could not capture a journal cursor"

    # THE root-launcher path: spawn-tier2 runs as ROOT (we are root) with
    # TIER2_ROOT_LAUNCHER=1 and the admin target uid. spawn-tier2 keeps root
    # only as the trusted launcher parent + dir bookkeeping; every podman op
    # (incl. the final run) is dropped to admin via runuser. NB we do NOT set
    # QDWIN_SECCTX_OPEN / QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED — acceptance must
    # rest on the PRODUCTION root-parent attestation, not a dev override.
    TIER2_ROOT_LAUNCHER=1 TIER2_ADMIN_UID="$ADMIN_UID" \
        WAYLAND_DISPLAY="$OUTER" \
        "$SPAWN" --disposable "$WORKLOAD" -- weston-terminal \
        >"$out" 2>"$err" &
    SPAWN_PID=$!

    # ---- identity the SHIPPED binary computes (app_id + launch token) -------
    local appid="" token=""
    for _ in $(seq 1 60); do
        container=$(awk -F= '/^CONTAINER=/{print $2; exit}' "$out" 2>/dev/null)
        appid=$(awk -F= '/^APP_ID=/{print $2; exit}' "$out" 2>/dev/null)
        token=$(awk -F= '/^LAUNCH_TOKEN=/{print $2; exit}' "$out" 2>/dev/null)
        [ -n "$container" ] && [ -n "$appid" ] && [ -n "$token" ] && break
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 0.5
    done
    if [ -z "$appid" ] || [ -z "$token" ]; then
        echo "--- spawn stderr ---" >&2; cat "$err" >&2
        fail wiretag "spawn emitted no APP_ID/LAUNCH_TOKEN within 30s (root-launcher path)"
    fi
    [[ "$appid" =~ $APPID_RE ]] \
        || fail wiretag "app_id '$appid' is not a qdistro.disp.<token> id"
    [[ "$token" =~ $TOKEN_RE ]] \
        || fail wiretag "launch token '$token' is not 32 hex (instance_id)"
    pass "root-launcher spawn computed disp app_id ($appid) + instance ($token)"

    # Guard: the root-launcher path must NOT have downgraded to the un-tagged
    # admin-direct fallback. If it warns "running un-tagged" the wire tag would
    # be absent and the whole point is lost — fail loudly.
    if grep -q 'running un-tagged' "$err"; then
        echo "--- spawn stderr ---" >&2; cat "$err" >&2
        fail wiretag "root-launcher spawn ran UN-TAGGED (it should have stamped the wire tag)"
    fi
    pass "root-launcher path did not downgrade to the un-tagged fallback"

    # ---- THE WIRE TAG: qdwin's secctx commit handler logged the disp app_id -
    # This is the load-bearing assertion. qdwin only logs this AFTER it accepts
    # the secctx-exec helper's manager bind (root-parent attestation) and the
    # client commits the triple. engine=qdistro.tier2 (a disposable is still a
    # tier-2 container), app_id is the exact disp id, instance_id is the launch
    # token. We require all three on ONE line so a stale/foreign commit cannot
    # satisfy it.
    local commit="" deadline=$(( $(date +%s) + 40 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        commit=$(journalctl --after-cursor="$cursor" 2>/dev/null \
            | grep -m1 -E "qdwin/secctx: committed engine=qdistro\.tier2 app_id=${appid//./\\.} instance_id=${token}" \
            || true)
        [ -n "$commit" ] && break
        kill -0 "$SPAWN_PID" 2>/dev/null || { sleep 1; \
            commit=$(journalctl --after-cursor="$cursor" 2>/dev/null \
                | grep -m1 -E "qdwin/secctx: committed engine=qdistro\.tier2 app_id=${appid//./\\.} instance_id=${token}" || true); \
            break; }
        sleep 0.5
    done
    if [ -z "$commit" ]; then
        echo "--- spawn stderr ---" >&2; cat "$err" >&2
        echo "--- recent qdwin secctx journal ---" >&2
        journalctl --after-cursor="$cursor" 2>/dev/null | grep -i 'secctx' | tail -20 >&2
        fail wiretag "qdwin never logged 'committed engine=qdistro.tier2 app_id=$appid instance_id=$token' — the disposable secctx app_id did NOT reach the compositor on the wire"
    fi
    echo "$commit" >&2
    pass "qdwin received the disposable secctx app_id ON THE WIRE (committed engine=qdistro.tier2 app_id=$appid instance_id=$token)"

    # ---- no rootful-podman confusion ---------------------------------------
    # The inner container must exist in ADMIN's rootless store, NOT root's. A
    # container in root's store would mean spawn-tier2 ran podman as root —
    # breaking the --userns=keep-id / admin-state model the design forbids.
    # Poll for registration: qdwin logs the secctx commit (asserted above) the
    # moment the secctx-exec helper binds, but admin's rootless podman registers
    # the container a beat later. Under the 16-wide full gate that lag exceeded a
    # single one-shot check and flaked the lane (idle always passed). A bounded
    # poll tolerates the lag WITHOUT weakening the assertion — a container that
    # never lands in admin's store still fails after the deadline. Mirrors the
    # commit poll above and the seq-based waits elsewhere in this probe.
    local admin_has=""
    for _ in $(seq 1 40); do
        if as_admin podman container exists "$container" 2>/dev/null; then
            admin_has=1; break
        fi
        sleep 0.5
    done
    [ -n "$admin_has" ] \
        || fail wiretag "container '$container' not in admin's rootless podman (did the run drop to admin?)"
    pass "inner container lives in admin's rootless podman (no rootful run)"
    if podman container exists "$container" 2>/dev/null; then
        fail wiretag "container '$container' ALSO exists in ROOT's podman store — rootful confusion (spawn ran podman as root)"
    fi
    pass "container absent from root's podman store (rootless drop confirmed)"

    # ---- teardown -----------------------------------------------------------
    as_admin podman stop -t 5 "$container" >/dev/null 2>&1 || true
    wait "$SPAWN_PID" 2>/dev/null || true
    SPAWN_PID=""
    local gone=""
    for _ in $(seq 1 40); do
        if ! as_admin podman container exists "$container" 2>/dev/null; then gone=1; break; fi
        sleep 0.5
    done
    [ -n "$gone" ] || fail wiretag "disposable '$container' survived close (--rm did not discard it)"
    container=""
    pass "close -> container gone (--rm discarded it)"

    pass wiretag
}

# Root-launcher mode exists ONLY to stamp the wire tag, so it must FAIL CLOSED
# (mint no container) when it cannot — secctx disabled, or the helper absent.
# A silent un-tagged fall-through would launch a disposable that looks tagged
# (right name/app_id on stdout) but carries no wp_security_context on the wire.
cmd_fail_closed() {
    clean_disp
    local out err rc leaked

    # (a) TIER2_USE_SECCTX=0 in root-launcher mode must be refused.
    out=$(mktemp); err=$(mktemp)
    timeout 30 \
        env TIER2_ROOT_LAUNCHER=1 TIER2_ADMIN_UID="$ADMIN_UID" \
            TIER2_USE_SECCTX=0 WAYLAND_DISPLAY="$OUTER" \
        "$SPAWN" --disposable "$WORKLOAD" -- weston-terminal >"$out" 2>"$err"
    rc=$?
    [ "$rc" -eq 124 ] && { as_admin podman rm -f "disp-${WORKLOAD}-"* >/dev/null 2>&1 || true; \
        fail fail-closed "root-launcher+USE_SECCTX=0 did not return (timed out) — likely launched un-tagged"; }
    [ "$rc" -ne 0 ] \
        || { echo "--- stdout ---" >&2; cat "$out" >&2; \
             fail fail-closed "root-launcher+USE_SECCTX=0 SUCCEEDED (rc=0) — fail-open un-tagged launch"; }
    grep -q 'requires TIER2_USE_SECCTX=1' "$err" \
        || { echo "--- stderr ---" >&2; cat "$err" >&2; \
             fail fail-closed "root-launcher+USE_SECCTX=0 did not fail with the expected refusal"; }
    leaked=$(as_admin podman ps -a --filter label=qdistro_disposable=1 \
             --format '{{.Names}}' 2>/dev/null | grep -E "^disp-${WORKLOAD}-" || true)
    [ -z "$leaked" ] || fail fail-closed "a disposable was minted despite USE_SECCTX=0 refusal: $leaked"
    rm -f "$out" "$err"
    pass "root-launcher refuses TIER2_USE_SECCTX=0 (no un-tagged launch, no container minted)"

    # (b) secctx-exec missing from PATH in root-launcher mode must be refused.
    # spawn-tier2 needs many tools before AND at the secctx-availability gate, so
    # we can't just strip PATH. Instead build a symlink farm of every tool
    # spawn-tier2 uses EXCEPT qdistro-secctx-exec, and point PATH at ONLY it —
    # so `command -v qdistro-secctx-exec` (the gate) comes up empty while
    # everything else still resolves. The early root-launcher gate then fires
    # the PACKAGING-GAP refusal before any container is minted.
    local farm; farm=$(mktemp -d); chmod 0755 "$farm"
    local t src
    for t in bash sh env readlink dirname basename od date tr grep awk sed \
             mkdir rm chmod stat id runuser podman dbus-send seq cat sleep \
             gunzip gzip base64 sha256sum command timeout install systemctl; do
        src=$(command -v "$t" 2>/dev/null) || continue
        ln -sf "$src" "$farm/$t"
    done
    # Sanity: the farm must NOT expose the helper but MUST expose the basics.
    if PATH="$farm" command -v qdistro-secctx-exec >/dev/null 2>&1; then
        echo "NOTE: helper still reachable via the farm; skipping missing-helper negative" >&2
    elif ! PATH="$farm" command -v podman >/dev/null 2>&1; then
        echo "NOTE: farm missing podman; skipping missing-helper negative" >&2
    else
        out=$(mktemp); err=$(mktemp)
        timeout 30 \
            env PATH="$farm" TIER2_ROOT_LAUNCHER=1 TIER2_ADMIN_UID="$ADMIN_UID" \
                WAYLAND_DISPLAY="$OUTER" \
            "$SPAWN" --disposable "$WORKLOAD" -- weston-terminal >"$out" 2>"$err"
        rc=$?
        [ "$rc" -ne 0 ] \
            || { echo "--- stdout ---" >&2; cat "$out" >&2; \
                 fail fail-closed "root-launcher with secctx-exec absent SUCCEEDED (rc=0) — fail-open"; }
        grep -q 'qdistro-secctx-exec is not in PATH' "$err" \
            || { echo "--- stderr ---" >&2; cat "$err" >&2; \
                 fail fail-closed "root-launcher missing-helper did not fail with the expected PACKAGING GAP refusal"; }
        leaked=$(as_admin podman ps -a --filter label=qdistro_disposable=1 \
                 --format '{{.Names}}' 2>/dev/null | grep -E "^disp-${WORKLOAD}-" || true)
        [ -z "$leaked" ] || fail fail-closed "a disposable was minted despite missing secctx-exec: $leaked"
        rm -f "$out" "$err"
        pass "root-launcher refuses a missing qdistro-secctx-exec (no un-tagged launch, no container minted)"
    fi
    rm -rf "$farm" 2>/dev/null || true

    clean_disp
    pass fail-closed
}

cmd_teardown() {
    clean_disp
    rm -f "$RULE_FILE" 2>/dev/null || true
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    rm -rf "$TIER2_BUILD_DIR" /tmp/disp-wiretag-build.log 2>/dev/null || true
    pass teardown
}

case "${1:-}" in
    setup) cmd_setup ;;
    wiretag) cmd_wiretag ;;
    fail-closed) cmd_fail_closed ;;
    teardown) cmd_teardown ;;
    *) echo "usage: $0 {setup|wiretag|fail-closed|teardown}" >&2; exit 2 ;;
esac
