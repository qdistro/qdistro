#!/bin/bash
# disp-probe — the REAL disposable tier-2 silo lifecycle (07-disposables-plan
# P1, M3 VM residual). The host lane tests/unit/test_disposables.py proves the
# pure helpers + reaper logic against a FAKE ops; this probe swaps in real
# podman on a live qdwin session — the half the headless dev host could not run
# (rootless podman + CAP_SYS_ADMIN + a running compositor).
#
# It drives the SHIPPED artifact under test — /usr/bin/qdistro-tier2-spawn
# (installed by install-qdwin-session-for-vm.sh) with its --disposable flag —
# NOT a copy from the source tree, so a packaging gap (the binary or the
# spawn-common library missing from a stock image) surfaces here. The reaper
# half imports the INSTALLED daemon module (/usr/libexec/qdistro) and exercises
# the real _SystemOps.disp_container_list / disp_container_remove against real
# podman, the podman-label filter + name-shape guard the fake ops cannot prove.
#
# Runs as root INSIDE the test VM (staged to /root by fresh-vm-bootstrap.sh):
# the disposable runs in admin's rootless podman, so the probe shells to admin
# via `runuser` (needs root), and the reaper's `runuser -u admin -- podman`
# calls likewise need root. Every PASS line is asserted by the bats wrapper
# (disposables-e2e.bats).
#
# Identifiers proven (D15): container name disp-<workload>-<YYYYMMDD-HHMMSS>,
# secctx app_id qdistro.disp.<token>, label qdistro_disposable=1, tmpfs
# /home/admin, podman --rm (AutoRemove). The broker spawn-gate action
# qdistro.dispose.spawn:<workload> is the same rules-only, fail-closed namespace
# as tier2.spawn — proven by an allow path (rule authored in setup) and a deny
# path (no rule -> broker "unknown" -> spawn refused, no container minted).
#
# SCOPE NOTE on secctx wire-tagging: spawn-tier2 only stamps the
# wp_security_context_v1 app_id (qdistro.disp.<token>) when launched by a
# root-trusted launcher (id==0 or the QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED dev
# override); an admin-driven spawn like this lane runs UN-TAGGED on the wire
# (spawn-tier2.sh:651-675). So this lane proves the disposable IDENTITY the
# shipped binary COMPUTES (the disp-* name + the qdistro.disp.<token> app_id it
# emits and would hand to secctx-exec) and that a real disposable WINDOW reaches
# the outer compositor from admin's container — but it does NOT assert the
# compositor received the disp app_id on the wire (that needs the root-launcher
# path, covered separately by phase7-secctx). The lane surfaces the un-tagged
# state as a NOTE so the scope stays visible rather than silently hidden.
set -u

SRC=/root/qdistro-src/qdistro
SPAWN=/usr/bin/qdistro-tier2-spawn          # the SHIPPED artifact under test
LIBEXEC=/usr/libexec/qdistro                # installed daemon modules (reaper)
ADMIN=admin
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER=wayland-1
WORKLOAD=weston-terminal                    # reuses the s32 tier-2 image
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
DENY_WL=denyme                              # image present, no broker rule
DENY_IMAGE="qdistro/tier2-${DENY_WL}:latest"
RULE_DIR=/etc/qdistro/rules.d
RULE_FILE="$RULE_DIR/zz-disp-${WORKLOAD}-allow.yaml"
# A build-helper copy admin can read (/root is 0700, the source tree lives
# under it). Only the IMAGE BUILD uses the source tree; the spawn under test
# always goes through the installed /usr/bin/qdistro-tier2-spawn.
TIER2_BUILD_DIR=/tmp/qd-disp-tier2

NAME_RE='^disp-weston-terminal-[0-9]{8}-[0-9]{6}(-[0-9a-f]{1,8})?$'
APPID_RE='^qdistro\.disp\.[0-9a-f]{8,64}$'

fail() { printf 'FAIL: %s — %s\n' "$1" "${2:-}" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$1"; }
# Pin XDG_RUNTIME_DIR so admin's rootless podman + spawn-tier2 always resolve
# /run/user/1000 regardless of the host->VM transport (under SSH, pam_systemd
# would otherwise hand root's /run/user/0 down through runuser — s32 foot-gun).
as_admin() { runuser -u "$ADMIN" -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" "$@"; }

broker_check() { # <action> -> prints the literal verdict token (allow/deny/unknown)
    as_admin dbus-send --system --print-reply=literal \
        --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckPermission \
        "string:$1" "dict:string:string:" 2>/dev/null | tr -d ' \t\n'
}

# Remove every disposable-LABELLED container in admin's podman (the reaper's
# own target set), plus any named test fixtures this probe creates. Best-effort
# — scoped to THIS VM's admin podman, so a concurrent agent (separate VM) is
# untouched. Never reaps an admin container by name shape alone.
clean_disp() {
    local n
    for n in $(as_admin podman ps -a --filter label=qdistro_disposable=1 \
               --format '{{.Names}}' 2>/dev/null); do
        as_admin podman rm -f "$n" >/dev/null 2>&1 || true
    done
    # Named, intentionally-UNLABELLED fixtures the reaper test creates as
    # negatives (the label filter never lists them, so the loop above can't).
    for n in $(as_admin podman ps -a \
               --format '{{.Names}}' 2>/dev/null \
               | grep -E '^(keepme|disp-keepme|forged)-' || true); do
        as_admin podman rm -f "$n" >/dev/null 2>&1 || true
    done
}

cmd_setup() {
    command -v podman >/dev/null 2>&1 || fail setup "podman not installed in this VM"
    command -v dbus-send >/dev/null 2>&1 || fail setup "dbus-send absent (broker gate cannot be queried)"
    [ -x "$SPAWN" ] || fail setup "$SPAWN not installed (install-qdwin-session-for-vm.sh ships it) — PACKAGING GAP"
    [ -f /usr/lib/qdistro/spawn-common.sh ] || fail setup "/usr/lib/qdistro/spawn-common.sh missing — PACKAGING GAP"
    [ -f "$LIBEXEC/qdistro_session_manager.py" ] || fail setup "$LIBEXEC/qdistro_session_manager.py missing — session manager not installed"
    [ -f "$LIBEXEC/qdistro_disposables.py" ] || fail setup "$LIBEXEC/qdistro_disposables.py missing — disposables module not installed (PACKAGING GAP)"

    # Outer admin compositor up? spawn-tier2 hard-requires the outer wayland
    # socket; without it NO tier-2 silo (disposable or not) can launch.
    as_admin test -S "$RUNTIME_DIR/$OUTER" \
        || fail setup "outer admin compositor not up ($RUNTIME_DIR/$OUTER missing)"

    # The broker must be live for the spawn-gate dbus-send to resolve.
    systemctl start qdistro-admin-broker.service 2>/dev/null || true

    # Build the tier-2 weston-terminal image (cached after first run) from the
    # source build helper, reachable by admin under /tmp.
    rm -rf "$TIER2_BUILD_DIR" 2>/dev/null || true
    cp -r "$SRC/tier2" "$TIER2_BUILD_DIR" || fail setup "stage tier2 build dir"
    chmod -R a+rX "$TIER2_BUILD_DIR"
    find "$TIER2_BUILD_DIR" -name '*.sh' -exec chmod a+rx {} +
    if ! as_admin podman image exists "$IMAGE" 2>/dev/null; then
        as_admin bash "$TIER2_BUILD_DIR/make-tier2-image.sh" "$WORKLOAD" \
            >/tmp/disp-build.log 2>&1 \
            || { cat /tmp/disp-build.log >&2; fail setup "build of $IMAGE failed"; }
    fi
    as_admin podman image exists "$IMAGE" || fail setup "$IMAGE not present after build"

    # Deny-path fixture: a workload whose IMAGE exists (so the spawn reaches the
    # broker gate, not the earlier image-exists check) but for which NO allow
    # rule is ever authored. A plain tag of the same image suffices.
    as_admin podman tag "$IMAGE" "$DENY_IMAGE" 2>/dev/null || true
    as_admin podman image exists "$DENY_IMAGE" || fail setup "could not tag $DENY_IMAGE"

    # Author the broker allow rule for the disposable spawn action. The
    # disposable namespace is rules-only/fail-closed: only an explicit admin
    # rule may authorize a throwaway silo (a cache row or hook verdict is
    # ignored by the broker for this action). No rule is authored for
    # $DENY_WL — that absence is what the deny-path test asserts.
    install -d -m 0755 "$RULE_DIR"
    cat >"$RULE_FILE" <<EOF
# Test-authored: allow the disposable spawn of the $WORKLOAD workload so the
# M3 VM lane can exercise the real launch path. disp-probe.sh / setup.
- name: disp-${WORKLOAD}-allow
  decision: allow
  match:
    action: qdistro.dispose.spawn:${WORKLOAD}
EOF
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    # Settle: the broker reloads rules on SIGHUP/restart; poll as the REAL
    # caller (admin uid) until the freshly-authored rule resolves to allow.
    local reply=""
    for _ in $(seq 1 20); do
        reply=$(broker_check "qdistro.dispose.spawn:${WORKLOAD}")
        [ "$reply" = "allow" ] && break
        sleep 0.25
    done
    [ "$reply" = "allow" ] \
        || fail setup "broker did not load the disp allow rule (CheckPermission='$reply')"

    clean_disp
    # Guard: nothing disposable-labelled may survive cleanup. A survivor is a
    # stuck container that would make a later assertion ambiguous — fail loudly
    # now rather than debug a confusing test failure later.
    local remaining
    remaining=$(as_admin podman ps -a --filter label=qdistro_disposable=1 \
                --format '{{.Names}}' 2>/dev/null)
    [ -z "$remaining" ] || fail setup "disposable containers survived cleanup (stuck?): $remaining"
    pass setup
}

cmd_spawn_window_close() {
    clean_disp
    # NB: out/err/container/SPAWN_PID are intentionally GLOBAL so the EXIT trap
    # below still sees them when `fail` exits mid-function.
    out=$(mktemp); err=$(mktemp)
    container=""; SPAWN_PID=""
    # EXIT trap so a `fail` (which exits) never strands a running disposable or
    # the backgrounded spawn tree, and the temp files always get cleaned.
    # shellcheck disable=SC2317
    _swc_cleanup() {
        [ -n "${container:-}" ] && as_admin podman rm -f "$container" >/dev/null 2>&1
        [ -n "${SPAWN_PID:-}" ] && kill "$SPAWN_PID" 2>/dev/null
        rm -f "$out" "$err" 2>/dev/null
        return 0
    }
    trap _swc_cleanup EXIT

    # Journal cursor so the window assertion only sees lines from THIS launch.
    # A valid cursor is load-bearing (we refuse to widen to a time window that
    # could match a stale advertise) — fail if the journal gave us none.
    local cursor
    cursor=$(journalctl --since=now -n0 --show-cursor 2>/dev/null \
             | awk -F': ' '/-- cursor:/ {print $2}')
    [ -n "$cursor" ] || fail spawn-window-close "could not capture a journal cursor (cannot scope the window assertion)"

    # Background the spawn in THIS shell (not a subshell) so $! is a real child
    # we can kill/wait. The container is --rm; it lives until we stop it.
    as_admin "$SPAWN" --disposable "$WORKLOAD" -- weston-terminal \
        >"$out" 2>"$err" &
    SPAWN_PID=$!

    # ---- identity the SHIPPED binary computes: name + app_id from stdout -----
    local appid=""
    for _ in $(seq 1 60); do
        container=$(awk -F= '/^CONTAINER=/{print $2; exit}' "$out" 2>/dev/null)
        appid=$(awk -F= '/^APP_ID=/{print $2; exit}' "$out" 2>/dev/null)
        [ -n "$container" ] && [ -n "$appid" ] && break
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 0.5
    done
    if [ -z "$container" ]; then
        echo "--- spawn stderr ---" >&2; cat "$err" >&2
        fail spawn-window-close "spawn emitted no CONTAINER= within 30s"
    fi
    [[ "$container" =~ $NAME_RE ]] \
        || fail spawn-window-close "container name '$container' is not a well-formed disp-* name ($NAME_RE)"
    pass "disp name shape ($container)"
    [[ "$appid" =~ $APPID_RE ]] \
        || fail spawn-window-close "app_id '$appid' is not a qdistro.disp.<token> id ($APPID_RE)"
    pass "secctx app_id ($appid)"
    # Make the wire-tagging scope visible (see SCOPE NOTE in the header). An
    # admin-driven spawn is expected to run un-tagged; this is a NOTE, not a
    # failure — the lane proves the COMPUTED identity, not the wire tag.
    grep -q 'running un-tagged' "$err" \
        && echo "NOTE: admin-driven spawn ran secctx un-tagged (expected; wire-tag needs the root-launcher path, covered by phase7-secctx)" >&2

    # ---- container present with the disposable isolation surface ------------
    local up=""
    for _ in $(seq 1 40); do
        if as_admin podman container exists "$container" 2>/dev/null; then up=1; break; fi
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 0.5
    done
    [ -n "$up" ] || { echo "--- spawn stderr ---" >&2; cat "$err" >&2; \
        fail spawn-window-close "container '$container' never appeared in podman"; }

    # Authoritative disposable marker is the LABEL (the reaper filters on it,
    # not the name). --rm (AutoRemove) is what makes discard by-construction.
    # Read the structured fields host-side via --format so we never have to
    # `podman exec` into the seccomp-hardened, no-new-privileges container.
    local label autorm tmpfs_opts
    label=$(as_admin podman inspect --format \
        '{{index .Config.Labels "qdistro_disposable"}}' "$container" 2>/dev/null)
    [ "$label" = "1" ] \
        || fail spawn-window-close "container missing qdistro_disposable=1 label (got '$label')"
    pass "qdistro_disposable=1 label present"
    autorm=$(as_admin podman inspect --format '{{.HostConfig.AutoRemove}}' "$container" 2>/dev/null)
    [ "$autorm" = "true" ] \
        || fail spawn-window-close "container is not --rm (AutoRemove='$autorm') — discard would leak"
    pass "podman --rm (AutoRemove) set"

    # tmpfs /home/admin: every byte of a disposable home lives in RAM and is
    # gone on teardown by construction (tmpfs + --rm), never the host fs. A
    # `--mount type=tmpfs` lands in podman's .HostConfig.Tmpfs map (NOT in
    # .Mounts, which stays empty for tmpfs), keyed by the destination path.
    tmpfs_opts=$(as_admin podman inspect --format \
        '{{index .HostConfig.Tmpfs "/home/admin"}}' "$container" 2>/dev/null)
    case "$tmpfs_opts" in
        *size=*) ;;  # a real tmpfs option string -> the home is tmpfs
        *) fail spawn-window-close "/home/admin is not a tmpfs (HostConfig.Tmpfs[/home/admin]='$tmpfs_opts')" ;;
    esac
    pass "tmpfs /home/admin (no persistent state)"

    # ---- window: the disposable's GUI app reaches the OUTER compositor -------
    # The inner weston-terminal is a real Wayland client; qdwin's nested-mode
    # publisher advertises its xdg_toplevel to the outer shell. We require the
    # POST-CURSOR advertise line tagged origin_uid=1000 — i.e. it came from
    # admin's container (the nested publisher stamps the peer uid). After
    # clean_disp this is the only disposable running, so the first such line is
    # this window. (The advertise carries the inner app's app_id, not the disp
    # secctx app_id — see the SCOPE NOTE — so we bind via origin_uid, not it.)
    local adv="" deadline=$(( $(date +%s) + 40 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        adv=$(journalctl --after-cursor="$cursor" 2>/dev/null \
              | grep -m1 "qdwin: nested-toplevel advertise.*origin_uid=$ADMIN_UID" || true)
        [ -n "$adv" ] && break
        sleep 0.5
    done
    [ -n "$adv" ] \
        || fail spawn-window-close "no post-cursor 'qdwin: nested-toplevel advertise ... origin_uid=$ADMIN_UID' within 40s (disposable window never reached the outer compositor)"
    pass "disposable window advertised to the outer compositor (origin_uid=$ADMIN_UID)"
    # Deterministic corroboration: the inner GUI process is genuinely alive in
    # the disposable (read host-side via `podman top`, no exec into the
    # hardened container).
    as_admin podman top "$container" args 2>/dev/null | grep -q weston-terminal \
        || fail spawn-window-close "inner weston-terminal not running in the disposable"
    pass "inner GUI process (weston-terminal) alive in the disposable"

    # ---- close -> container gone (--rm teardown) ----------------------------
    as_admin podman stop -t 5 "$container" >/dev/null 2>&1 || true
    wait "$SPAWN_PID" 2>/dev/null || true
    SPAWN_PID=""
    local gone=""
    for _ in $(seq 1 40); do
        if ! as_admin podman container exists "$container" 2>/dev/null; then gone=1; break; fi
        sleep 0.5
    done
    [ -n "$gone" ] || fail spawn-window-close "disposable '$container' survived close (--rm did not discard it)"
    container=""
    pass "close -> container gone (--rm discarded it)"

    pass spawn-window-close
}

cmd_reaper_sweep() {
    clean_disp
    local ts orphan keep dispkeep forged
    ts=$(date +%Y%m%d-%H%M%S)
    orphan="disp-orphan-$ts"        # disp-named AND labelled  -> MUST be reaped
    keep="keepme-$ts"               # non-disp, unlabelled      -> ordinary container, survives
    dispkeep="disp-keepme-$ts"      # disp-NAMED but UNLABELLED  -> admin ctr merely named disp-*, survives
    forged="forged-$ts"             # LABELLED but non-disp name -> forgery; listed, but name-guard saves it

    # A real STRANDED disposable + the forgery negatives. `--entrypoint sleep`
    # so each is genuinely RUNNING (the image's own entrypoint would exit
    # without XDG_RUNTIME_DIR); a running orphan exercises rm -f kill semantics.
    as_admin podman run -d --name "$orphan" \
        --label qdistro_disposable=1 --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail reaper-sweep "could not create orphan $orphan"
    as_admin podman run -d --name "$keep" \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail reaper-sweep "could not create keep container $keep"
    as_admin podman run -d --name "$dispkeep" \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail reaper-sweep "could not create disp-named unlabelled container $dispkeep"
    # The forgery: a hostile image could bake `LABEL qdistro_disposable=1`, so
    # the label filter WILL list this — only the name-shape guard on the sweep
    # /remove path stands between it and rm -f.
    as_admin podman run -d --name "$forged" \
        --label qdistro_disposable=1 --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail reaper-sweep "could not create forged labelled container $forged"

    # Drive the REAL installed reaper collaborators: _SystemOps.disp_container_list
    # (real `podman ps --filter label=qdistro_disposable=1` as admin) +
    # qdistro_disposables.disp_sweep_targets (name-shape selection) +
    # _SystemOps.disp_container_remove (real `podman rm -f`, name-guarded).
    local py_out
    py_out=$(QDISTRO_ADMIN_USER="$ADMIN" XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        python3 - "$orphan" "$keep" "$dispkeep" "$forged" <<PY 2>&1
import sys
sys.path.insert(0, "$LIBEXEC")
import qdistro_session_manager as M
import qdistro_disposables as D
orphan, keep, dispkeep, forged = sys.argv[1:5]
ops = M._SystemOps()

# Layer 1 — the LABEL filter is what disp_container_list keys on.
listed = ops.disp_container_list()
assert orphan in listed,    f"orphan {orphan!r} not label-listed: {listed}"
assert forged in listed,    f"forged {forged!r} (labelled) not label-listed: {listed}"
assert keep not in listed,  f"unlabelled {keep!r} leaked into list: {listed}"
assert dispkeep not in listed, f"disp-NAMED-but-unlabelled {dispkeep!r} leaked into list: {listed}"
print("LIST_OK")

# Layer 2 — the NAME-shape guard is what disp_sweep_targets keys on. It must
# reject the forged (labelled but mis-named) container so it never reaches rm.
targets = D.disp_sweep_targets(listed)
assert orphan in targets,     f"orphan not a sweep target: {targets}"
assert forged not in targets, f"forged {forged!r} (bad name) IS a sweep target: {targets}"
print("TARGETS_OK")

# Remove the real orphan (rm -f a RUNNING container) via the real op.
assert ops.disp_container_remove(orphan), "remove(orphan) returned False"
print("REMOVE_OK")

# Defence in depth: the remove path itself refuses a non-disposable name even
# if one reached it (the forged container's label got it listed).
try:
    ops.disp_container_remove(forged)
    print("GUARD_FAIL")
except ValueError:
    print("GUARD_OK")
PY
)
    echo "$py_out" >&2
    echo "$py_out" | grep -q LIST_OK    || fail reaper-sweep "label filter wrong (orphan/forged not listed, or a non-labelled negative leaked in)"
    echo "$py_out" | grep -q TARGETS_OK || fail reaper-sweep "name-shape guard wrong (forged labelled container selected as a sweep target)"
    echo "$py_out" | grep -q REMOVE_OK  || fail reaper-sweep "real podman rm of the running orphan failed"
    echo "$py_out" | grep -q GUARD_OK   || fail reaper-sweep "remove path did not refuse a non-disposable name"
    pass "reaper lists by label + sweeps by name-shape; forged & misnamed containers refused"

    # Ground truth from podman: orphan gone; every negative survived.
    as_admin podman container exists "$orphan" 2>/dev/null \
        && fail reaper-sweep "orphan $orphan still present after reap"
    local n
    for n in "$keep" "$dispkeep" "$forged"; do
        as_admin podman container exists "$n" 2>/dev/null \
            || fail reaper-sweep "negative container $n was collateral-reaped (MUST survive)"
    done
    pass "orphan reaped; ordinary, disp-named-unlabelled, and forged-labelled containers all survived"

    for n in "$keep" "$dispkeep" "$forged"; do
        as_admin podman rm -f "$n" >/dev/null 2>&1 || true
    done
    pass reaper-sweep
}

cmd_deny_fail_closed() {
    clean_disp
    # First, directly assert the rules-only namespace returns "unknown" for the
    # unruled workload — this is the property the spawn refusal must rest on
    # (NOT a broker that happens to be down/unreachable).
    local verdict
    verdict=$(broker_check "qdistro.dispose.spawn:${DENY_WL}")
    [ "$verdict" = "unknown" ] \
        || fail deny-fail-closed "broker did not return 'unknown' for an unruled disposable (got '$verdict') — cannot trust the deny path"
    pass "broker returns 'unknown' for the unruled disposable action"

    # No allow rule exists for $DENY_WL. Its image IS present (tagged in setup),
    # so the spawn reaches the mandatory broker gate rather than failing earlier
    # on image-exists. The broker returns "unknown" (rules-only/fail-closed),
    # and spawn-tier2 MUST refuse — never mint a container off a missing rule.
    # `timeout` so a fail-OPEN regression (podman actually runs) cannot wedge
    # the lane: rc=124 is then itself fail-open evidence.
    local out err; out=$(mktemp); err=$(mktemp)
    timeout 30 \
        runuser -u "$ADMIN" -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        "$SPAWN" --disposable "$DENY_WL" -- weston-terminal >"$out" 2>"$err"
    local rc=$?
    if [ "$rc" -eq 124 ]; then
        as_admin podman rm -f "disp-${DENY_WL}-"* >/dev/null 2>&1 || true
        fail deny-fail-closed "spawn of an unruled disposable did not return (timed out) — fail-open: podman likely ran the container"
    fi
    [ "$rc" -ne 0 ] \
        || { echo "--- spawn stdout ---" >&2; cat "$out" >&2; \
             fail deny-fail-closed "spawn of an unruled disposable SUCCEEDED (rc=0) — fail-open!"; }
    # Require the EXACT rules-only verdict for THIS action — not a bare mention
    # of "broker" (which would also match a broker-unreachable error, a
    # different failure than the rules-only 'unknown' we mean to prove).
    grep -q 'decision=unknown' "$err" \
        || { echo "--- spawn stderr ---" >&2; cat "$err" >&2; \
             fail deny-fail-closed "spawn failed but NOT with the rules-only 'decision=unknown' verdict (broker may have been unreachable, not fail-closed)"; }
    grep -q "qdistro.dispose.spawn:${DENY_WL}" "$err" \
        || { echo "--- spawn stderr ---" >&2; cat "$err" >&2; \
             fail deny-fail-closed "the deny did not name the disposable action qdistro.dispose.spawn:${DENY_WL}"; }
    pass "broker has no rule -> spawn refused at the gate (decision=unknown)"

    # And no disp-denyme-* container was minted.
    local leaked
    leaked=$(as_admin podman ps -a --filter label=qdistro_disposable=1 \
             --format '{{.Names}}' 2>/dev/null | grep -E '^disp-denyme-' || true)
    [ -z "$leaked" ] || fail deny-fail-closed "a disposable container was minted despite the deny: $leaked"
    pass "no container minted on the deny path (fail-closed)"

    rm -f "$out" "$err"
    pass deny-fail-closed
}

cmd_teardown() {
    clean_disp
    rm -f "$RULE_FILE" 2>/dev/null || true
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    as_admin podman rmi "$DENY_IMAGE" >/dev/null 2>&1 || true
    rm -rf "$TIER2_BUILD_DIR" /tmp/disp-build.log 2>/dev/null || true
    pass teardown
}

case "${1:-}" in
    setup) cmd_setup ;;
    spawn-window-close) cmd_spawn_window_close ;;
    reaper-sweep) cmd_reaper_sweep ;;
    deny-fail-closed) cmd_deny_fail_closed ;;
    teardown) cmd_teardown ;;
    *) echo "usage: $0 {setup|spawn-window-close|reaper-sweep|deny-fail-closed|teardown}" >&2; exit 2 ;;
esac
