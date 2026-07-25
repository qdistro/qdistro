#!/bin/bash
# podapp-launch-wiretag-probe — prove a LAUNCHER CLICK on a container app
# reaches the outer compositor fully identified, through the production path
# that tracker J12 introduced:
#
#   qdshell (admin) --D-Bus--> SessionManager1.LaunchPodApp (root daemon)
#     -> systemctl start qdistro-podapp@<launch-token>.service (User=root)
#       -> qdistro-podapp-launch -> spawn-tier2 TIER2_ROOT_LAUNCHER=1
#         -> qdistro-secctx-exec stamps wp_security_context_v1 on the wire
#
# Before J12 the click forked spawn-tier2 DIRECTLY from the unprivileged
# qdshell session. With no root launcher parent spawn-tier2 took its un-tagged
# branch — the container's Wayland connection carried no security context at
# all — and on a hardened profile it refused the launch outright, so clicking a
# pod app did nothing whatsoever. This probe drives the SHIPPED daemon method,
# unit and helpers, with NO dev override (no QDWIN_SECCTX_OPEN, no
# QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED).
#
# Two deterministic journal signals, one per J12 drop point:
#
#   (Drop A, launch side) qdwin's own secctx commit handler:
#     qdwin/secctx: committed engine=qdistro.tier2 app_id=<container>/<app>
#                   instance_id=<launch-token>
#
#   (Drop B, compositor side) the nested-proxy secctx forward — tier-2 app
#   windows reach qdwin as NESTED PROXIES, and qdwin used to skip those
#   unconditionally, so the shell never learned which silo/app a tier-2 window
#   belonged to and its cold-start placeholder always timed out:
#     qdwin: toplevel_security_context (nested proxy) handle=<n>
#            engine=qdistro.tier2 app_id=<container>/<app> instance=<token>
#
# Both must carry the SAME token the D-Bus reply returned — that token is what
# the shell matches to resolve its launch placeholder, so a mismatch is the
# whole feature failing silently.
#
# Runs as root INSIDE the test VM (staged to /root). Every PASS line is
# asserted by podapp-launch-wiretag.bats.
set -u

SPAWN=/usr/bin/qdistro-tier2-spawn
UNIT_TMPL="qdistro-podapp@"
LAUNCH_HELPER=/usr/libexec/qdistro/qdistro-podapp-launch
STOP_HELPER=/usr/libexec/qdistro/qdistro-podapp-stop
ADMIN=admin
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
WORKLOAD=weston-terminal
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
LAUNCH_ENV_DIR=/run/qdistro/podapp-launch
BUS=org.qdistro.SessionManager1
OBJ=/org/qdistro/SessionManager1
RULE_DIR=/etc/qdistro/rules.d
RULE_FILE="$RULE_DIR/zz-podapp-wiretag-allow.yaml"
# spawn-tier2's broker gate keys on the WORKLOAD, not the container:
# qdistro.tier2.spawn:<workload>/<app>.
SPAWN_ACTION="qdistro.tier2.spawn:${WORKLOAD}/${WORKLOAD}"

# The pod-app container this probe launches. spawn-tier2 does
# `podman run --name <container>`, so it must not collide with a real one.
CONTAINER=podappwt

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

# Call LaunchPodApp AS ADMIN — the real caller is qdshell in the admin session,
# and the method is admin-gated, so calling it as root would prove nothing
# about the path a click actually takes.
launch_podapp() {
    as_admin gdbus call --system --dest "$BUS" --object-path "$OBJ" \
        --method "$BUS.LaunchPodApp" "$1" "$2" "$3"
}

clean_podapp() {
    local u
    # Stop every instance of the template this probe may have left behind.
    for u in $(systemctl list-units --all --no-legend "${UNIT_TMPL}*.service" 2>/dev/null \
               | awk '{print $1}'); do
        systemctl stop "$u" >/dev/null 2>&1 || true
        systemctl reset-failed "$u" >/dev/null 2>&1 || true
    done
    as_admin podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -f "$LAUNCH_ENV_DIR"/*.env 2>/dev/null || true
}

cmd_setup() {
    command -v podman  >/dev/null 2>&1 || fail setup "podman not installed"
    command -v gdbus   >/dev/null 2>&1 || fail setup "gdbus absent"
    command -v runuser >/dev/null 2>&1 || fail setup "runuser absent"
    [ -x "$SPAWN" ]         || fail setup "$SPAWN not installed — PACKAGING GAP"
    [ -x "$LAUNCH_HELPER" ] || fail setup "$LAUNCH_HELPER not installed — PACKAGING GAP"
    [ -x "$STOP_HELPER" ]   || fail setup "$STOP_HELPER not installed — PACKAGING GAP"
    [ -f "/etc/systemd/system/${UNIT_TMPL}.service" ] \
        || fail setup "pod-app launcher unit not installed — PACKAGING GAP"
    command -v qdistro-secctx-exec >/dev/null 2>&1 \
        || fail setup "qdistro-secctx-exec not installed — PACKAGING GAP"
    systemctl is-active --quiet qdistro-session-manager.service \
        || fail setup "qdistro-session-manager is not running"
    as_admin podman image exists "$IMAGE" 2>/dev/null \
        || fail setup "tier-2 image $IMAGE absent from admin's rootless store"
    # The method must exist on the SHIPPED daemon — a stale daemon would make
    # every assertion below fail for the wrong reason.
    gdbus introspect --system --dest "$BUS" --object-path "$OBJ" 2>/dev/null \
        | grep -q 'LaunchPodApp' \
        || fail setup "the running session manager has no LaunchPodApp method (stale daemon?)"
    # spawn-tier2 gates the launch on the broker's
    # qdistro.tier2.spawn:<workload>/<app> action, which the shipped rule set
    # does not allow by default. Author a test-owned allow rule (dropped in
    # teardown) exactly as tier2-silo-secctx-wiretag-probe.sh does — this lane
    # is about the LAUNCH TOPOLOGY, not about the broker's default policy.
    install -d -m 0755 "$RULE_DIR"
    cat >"$RULE_FILE" <<EOF
# Test-authored: allow the tier-2 spawn of $WORKLOAD so the pod-app launch lane
# can drive the root-launcher path. podapp-launch-wiretag.
- name: podapp-wiretag-${WORKLOAD}-allow
  decision: allow
  match:
    action: ${SPAWN_ACTION}
EOF
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    local reply=""
    for _ in $(seq 1 20); do
        reply=$(broker_check_admin "$SPAWN_ACTION")
        [ "$reply" = "allow" ] && break
        sleep 0.25
    done
    [ "$reply" = "allow" ] \
        || fail setup "broker did not load the pod-app allow rule (CheckPermission='$reply')"

    clean_podapp
    pass setup
}

cmd_teardown() {
    clean_podapp
    rm -f "$RULE_FILE" 2>/dev/null || true
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    pass teardown
}

# ---- the wire tag, both drops ---------------------------------------------
cmd_wiretag() {
    clean_podapp

    local cursor
    cursor=$(journalctl --since=now -n0 --show-cursor 2>/dev/null \
             | awk -F': ' '/-- cursor:/ {print $2}')
    [ -n "$cursor" ] || fail wiretag "could not capture a journal cursor"

    # THE CLICK. Admin -> D-Bus -> root daemon -> root unit.
    local reply
    reply=$(launch_podapp "$CONTAINER" "$WORKLOAD" "[\"$WORKLOAD\"]" 2>&1) \
        || fail wiretag "LaunchPodApp failed as admin: $reply"

    # gdbus prints the GVariant tuple ('<32 hex>',).
    local token
    token=$(printf '%s' "$reply" | sed -n "s/^('\([0-9a-f]\{32\}\)',)$/\1/p")
    [ -n "$token" ] \
        || fail wiretag "LaunchPodApp reply is not a launch-token tuple: $reply"
    [[ "$token" =~ $TOKEN_RE ]] || fail wiretag "token '$token' is not 32 hex"
    pass "LaunchPodApp returned a launch token ($token)"

    local unit="${UNIT_TMPL}${token}.service"
    local appid="${CONTAINER}/${WORKLOAD}"

    # ---- the unit ran, and spawn-tier2 honoured the PRE-COMMITTED token -----
    # This is the piece that lets the shell resolve its placeholder without
    # reading spawn-tier2's stdout (under a unit that is the journal, not a
    # pipe): the daemon chose the token, returned it, and spawn-tier2 must use
    # exactly that one as its secctx instance-id.
    local spawn_token="" deadline=$(( $(date +%s) + 40 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        spawn_token=$(journalctl -u "$unit" --after-cursor="$cursor" 2>/dev/null \
                      | sed -n 's/.*LAUNCH_TOKEN=\([0-9a-f]\{32\}\).*/\1/p' | head -1)
        [ -n "$spawn_token" ] && break
        sleep 0.5
    done
    [ -n "$spawn_token" ] \
        || { journalctl -u "$unit" --after-cursor="$cursor" | tail -40 >&2; \
             fail wiretag "unit $unit emitted no LAUNCH_TOKEN — did it start?"; }
    [ "$spawn_token" = "$token" ] \
        || fail wiretag "spawn-tier2 used token '$spawn_token' but the D-Bus reply promised '$token' — the shell would watch for a token that never arrives"
    pass "the unit ran and spawn-tier2 used the PRE-COMMITTED token ($token)"

    # Guard: the root-launcher path must NOT have downgraded to un-tagged.
    if journalctl -u "$unit" --after-cursor="$cursor" 2>/dev/null \
       | grep -q 'running un-tagged'; then
        journalctl -u "$unit" --after-cursor="$cursor" | tail -30 >&2
        fail wiretag "pod-app ran UN-TAGGED — this is exactly the J12 Drop A bug"
    fi
    pass "pod-app launch did not downgrade to the un-tagged fallback"

    # ---- Drop A: qdwin's secctx commit carries the app identity ------------
    local commit="" cdl=$(( $(date +%s) + 60 ))
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
        fail wiretag "qdwin never logged the pod-app secctx commit (app_id=$appid instance_id=$token) — the app_id did NOT reach the compositor on the wire"
    fi
    echo "$commit" >&2
    pass "qdwin received the pod-app secctx app_id ON THE WIRE (app_id=$appid instance_id=$token)"

    # ---- Drop B: the NESTED PROXY forward reaches the shell protocol -------
    # A tier-2 app window arrives as a nested proxy, and qdwin used to skip
    # proxies in qdwin_send_toplevel_security_context — so even a perfectly
    # tagged container produced no toplevel_security_context event and the
    # shell's placeholder timed out after 15s. Drop A passing while this fails
    # is precisely the state J12 found the tree in.
    local fwd="" fdl=$(( $(date +%s) + 60 ))
    while [ "$(date +%s)" -lt "$fdl" ]; do
        fwd=$(journalctl --after-cursor="$cursor" 2>/dev/null \
            | grep -m1 -E "qdwin: toplevel_security_context \(nested proxy\).*instance=${token}" \
            || true)
        [ -n "$fwd" ] && break
        sleep 0.5
    done
    if [ -z "$fwd" ]; then
        echo "--- recent qdwin toplevel journal ---" >&2
        journalctl --after-cursor="$cursor" 2>/dev/null \
            | grep -E 'nested-toplevel|toplevel_security_context' | tail -20 >&2
        fail wiretag "qdwin never forwarded toplevel_security_context for the nested proxy (instance=$token) — the shell cannot identify the window, so the launch placeholder times out (J12 Drop B)"
    fi
    echo "$fwd" >&2
    # The forwarded app_id must be the app's, not some other client's.
    printf '%s' "$fwd" | grep -q "app_id=${appid}" \
        || fail wiretag "the nested-proxy forward carried the wrong app_id: $fwd"
    pass "qdwin forwarded toplevel_security_context for the nested proxy (app_id=$appid instance=$token)"

    # ---- no rootful-podman confusion ---------------------------------------
    as_admin podman container exists "$CONTAINER" 2>/dev/null \
        || { journalctl -u "$unit" --after-cursor="$cursor" | tail -20 >&2; \
             fail wiretag "container '$CONTAINER' not in admin's rootless podman (did the run drop to admin?)"; }
    pass "pod-app container lives in admin's rootless podman (no rootful run)"
    if podman container exists "$CONTAINER" 2>/dev/null; then
        fail wiretag "container '$CONTAINER' ALSO exists in ROOT's podman store — rootful confusion"
    fi
    pass "pod-app container absent from root's podman store (rootless drop confirmed)"

    # ---- ExecStop tears the admin container down ---------------------------
    systemctl stop "$unit" >/dev/null 2>&1 || true
    local gone="" gdl=$(( $(date +%s) + 40 ))
    while [ "$(date +%s)" -lt "$gdl" ]; do
        if ! as_admin podman container exists "$CONTAINER" 2>/dev/null; then gone=1; break; fi
        sleep 0.5
    done
    [ -n "$gone" ] \
        || fail wiretag "container '$CONTAINER' survived 'systemctl stop' — the root-unit ExecStop did not drop to admin and orphaned it"
    pass "systemctl stop -> admin container gone (root-unit ExecStop dropped to admin)"

    # The stanza is single-use; the unit's ExecStopPost must drop it so /run
    # does not accumulate one spent root-owned file per click.
    local left
    left=$(ls -1 "$LAUNCH_ENV_DIR/${token}.env" 2>/dev/null || true)
    [ -z "$left" ] \
        || fail wiretag "spent launch stanza $left survived the unit — /run accumulates one per click"
    pass "spent launch stanza removed when the unit stopped"

    systemctl reset-failed "$unit" >/dev/null 2>&1 || true
    pass wiretag
}

# ---- fail-closed: refuse, never launch un-tagged ---------------------------
cmd_fail_closed() {
    clean_podapp
    local out rc

    # (1) The method is admin-gated. It starts a User=root unit, so a non-admin
    #     caller reaching it would be a privilege boundary hole. `nobody` stands
    #     in for any non-admin local uid.
    if id nobody >/dev/null 2>&1; then
        out=$(runuser -u nobody -- gdbus call --system --dest "$BUS" \
                --object-path "$OBJ" --method "$BUS.LaunchPodApp" \
                "$CONTAINER" "$WORKLOAD" '[]' 2>&1); rc=$?
        [ "$rc" -ne 0 ] \
            || fail fail-closed "LaunchPodApp SUCCEEDED for non-admin uid 'nobody' — a non-admin can start a root unit"
        pass "LaunchPodApp refuses a non-admin caller"
    else
        echo "NOTE: no 'nobody' account; skipped the non-admin refusal check" >&2
    fi

    # (2) The helper must refuse a launch env it cannot trust. It runs as root
    #     and `.`-sources the file, so an admin-writable stanza would be root
    #     code execution.
    local tok="deadbeefdeadbeefdeadbeefdeadbeef"
    install -d -m 0755 "$LAUNCH_ENV_DIR"
    cat >"$LAUNCH_ENV_DIR/${tok}.env" <<EOF
QD_LAUNCH_TOKEN='${tok}'
QD_CONTAINER='${CONTAINER}'
QD_WORKLOAD='${WORKLOAD}'
QD_APP_ARGV_JSON='["${WORKLOAD}"]'
EOF
    chown "$ADMIN_UID:$ADMIN_UID" "$LAUNCH_ENV_DIR/${tok}.env"
    out=$("$LAUNCH_HELPER" "$tok" 2>&1); rc=$?
    rm -f "$LAUNCH_ENV_DIR/${tok}.env"
    [ "$rc" -ne 0 ] \
        || fail fail-closed "the launch helper SOURCED an admin-owned env file as root"
    printf '%s' "$out" | grep -q 'refusing to source untrusted content' \
        || fail fail-closed "the launch helper refused for the wrong reason: $out"
    pass "launch helper REFUSES a non-root-owned (admin-writable) launch env"

    # (3) A token that is not the exact generated shape must be refused, not
    #     sanitised — it becomes a file path and the secctx instance-id.
    out=$("$LAUNCH_HELPER" "../../etc/shadow" 2>&1); rc=$?
    [ "$rc" -ne 0 ] || fail fail-closed "the launch helper accepted a path-shaped token"
    printf '%s' "$out" | grep -q '32 lowercase hex' \
        || fail fail-closed "the launch helper rejected a bad token for the wrong reason: $out"
    pass "launch helper REFUSES a malformed launch token"

    # (4) The stanza's token must agree with the unit instance — a mismatch
    #     means we are about to launch off another click's stanza.
    local tok2="cafebabecafebabecafebabecafebabe"
    cat >"$LAUNCH_ENV_DIR/${tok2}.env" <<EOF
QD_LAUNCH_TOKEN='deadbeefdeadbeefdeadbeefdeadbeef'
QD_CONTAINER='${CONTAINER}'
QD_WORKLOAD='${WORKLOAD}'
QD_APP_ARGV_JSON='["${WORKLOAD}"]'
EOF
    chown 0:0 "$LAUNCH_ENV_DIR/${tok2}.env"
    chmod 0600 "$LAUNCH_ENV_DIR/${tok2}.env"
    out=$("$LAUNCH_HELPER" "$tok2" 2>&1); rc=$?
    rm -f "$LAUNCH_ENV_DIR/${tok2}.env"
    [ "$rc" -ne 0 ] \
        || fail fail-closed "the launch helper launched with a stanza whose token does not match its instance"
    printf '%s' "$out" | grep -q 'does not match' \
        || fail fail-closed "the token-mismatch refusal reported the wrong reason: $out"
    pass "launch helper REFUSES a stanza whose token does not match the unit instance"

    # (5) spawn-tier2 must not accept a malformed pre-committed token either
    #     (the helper is not the only possible caller of that env knob).
    out=$(TIER2_LAUNCH_TOKEN="not-a-token" "$SPAWN" "$CONTAINER" "$WORKLOAD" \
            -- "$WORKLOAD" 2>&1); rc=$?
    [ "$rc" -ne 0 ] || fail fail-closed "spawn-tier2 accepted a malformed TIER2_LAUNCH_TOKEN"
    printf '%s' "$out" | grep -q '32 lowercase hex' \
        || fail fail-closed "spawn-tier2 rejected a bad TIER2_LAUNCH_TOKEN for the wrong reason: $out"
    pass "spawn-tier2 REFUSES a malformed pre-committed launch token"

    clean_podapp
    pass "fail-closed"
}

case "${1:-}" in
    setup) cmd_setup ;;
    wiretag) cmd_wiretag ;;
    fail-closed) cmd_fail_closed ;;
    teardown) cmd_teardown ;;
    *) echo "usage: $0 {setup|wiretag|fail-closed|teardown}" >&2; exit 2 ;;
esac
