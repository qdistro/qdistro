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
# SCOPE NOTE on secctx wire-tagging: this ADMIN-driven lane runs UN-TAGGED on
# the wire. spawn-tier2 stamps the wp_security_context_v1 app_id
# (qdistro.disp.<token>) only via the ROOT-launcher path (TIER2_ROOT_LAUNCHER=1,
# which runs qdistro-secctx-exec under a root `runuser` parent so qdwin's
# hardened secctx authorization accepts the manager bind); an admin-driven spawn
# has no such root parent and correctly runs un-tagged (spawn-tier2.sh, the
# admin-direct branch). So THIS lane proves the disposable IDENTITY the shipped
# binary COMPUTES (the disp-* name + the qdistro.disp.<token> app_id it emits and
# would hand to secctx-exec) and that a real disposable WINDOW reaches the outer
# compositor from admin's container — but it does NOT assert the compositor
# received the disp app_id ON THE WIRE. That wire tag is proven by the dedicated
# root-launcher lane disposable-secctx-wiretag.bats
# (probes/disp-secctx-wiretag-probe.sh). This lane surfaces the un-tagged state
# as a NOTE so the scope stays visible rather than silently hidden.
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
    py_out=$(XDG_RUNTIME_DIR="$RUNTIME_DIR" \
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

# A 32-char lowercase-hex per-spawn token (the shape spawn-common gen_launch_token
# emits and is_disposable_token accepts), for the synthetic lease fixtures.
gen_token() { od -An -N16 -tx1 /dev/urandom | tr -d ' \n'; }

cmd_lease_spawn_labels() {
    # Prove the SHIPPED /usr/bin/qdistro-tier2-spawn stamps the TTL-lease labels
    # on the REAL container when QDISTRO_DISPOSABLE_TTL is opted in — the
    # spawn-side half of the lease (the daemon-side reap is cmd_lease_sweep).
    # We only need the container to be CREATED with its labels; no window wait.
    clean_disp
    local out err container ttl created label_ttl label_created
    out=$(mktemp); err=$(mktemp); container=""; SPAWN_PID=""
    # shellcheck disable=SC2317
    _lsl_cleanup() {
        [ -n "${container:-}" ] && as_admin podman rm -f "$container" >/dev/null 2>&1
        [ -n "${SPAWN_PID:-}" ] && kill "$SPAWN_PID" 2>/dev/null
        rm -f "$out" "$err" 2>/dev/null
        return 0
    }
    trap _lsl_cleanup EXIT

    ttl=3600   # long: this disposable must NOT be reaped by an incidental sweep
    as_admin env QDISTRO_DISPOSABLE_TTL="$ttl" \
        "$SPAWN" --disposable "$WORKLOAD" -- weston-terminal \
        >"$out" 2>"$err" &
    SPAWN_PID=$!
    for _ in $(seq 1 60); do
        container=$(awk -F= '/^CONTAINER=/{print $2; exit}' "$out" 2>/dev/null)
        [ -n "$container" ] && as_admin podman container exists "$container" 2>/dev/null && break
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 0.5
    done
    [ -n "$container" ] && as_admin podman container exists "$container" 2>/dev/null \
        || { echo "--- spawn stderr ---" >&2; cat "$err" >&2; \
             fail lease-spawn-labels "disposable container never appeared"; }

    label_ttl=$(as_admin podman inspect --format \
        '{{index .Config.Labels "qdistro_lease_ttl"}}' "$container" 2>/dev/null)
    [ "$label_ttl" = "$ttl" ] \
        || fail lease-spawn-labels "qdistro_lease_ttl label '$label_ttl' != $ttl"
    pass "shipped spawn stamped qdistro_lease_ttl=$ttl"
    label_created=$(as_admin podman inspect --format \
        '{{index .Config.Labels "qdistro_lease_created"}}' "$container" 2>/dev/null)
    [[ "$label_created" =~ ^[0-9]+$ ]] \
        || fail lease-spawn-labels "qdistro_lease_created label '$label_created' is not an epoch integer"
    pass "shipped spawn stamped qdistro_lease_created=$label_created (epoch)"

    as_admin podman rm -f "$container" >/dev/null 2>&1 || true
    container=""
    pass lease-spawn-labels
}

cmd_lease_sweep() {
    # Drive the REAL daemon-side TTL-lease machinery against real podman:
    # _SystemOps.disp_lease_candidates (real `podman ps --format json` label read,
    # where an absent label is None) + qdistro_disposables.lease_sweep_targets
    # + _SiloStore.sweep_expired_leases -> dispose() -> real `podman rm -f`. The
    # host fake-ops cannot prove the podman label parsing or the real rm.
    clean_disp
    local ts now past tok_exp tok_fresh tok_nolease tok_forged
    ts=$(date +%Y%m%d-%H%M%S)
    now=$(date +%s); past=$(( now - 100 ))
    tok_exp=$(gen_token); tok_fresh=$(gen_token)
    tok_nolease=$(gen_token); tok_forged=$(gen_token)

    local exp fresh nolease notoken forged
    exp="disp-lexp-$ts"        # expired lease (ttl 1, created 100s ago) -> REAP
    fresh="disp-lfresh-$ts"    # long ttl, created now                   -> keep
    nolease="disp-lnone-$ts"   # disposable, valid token, NO lease labels -> keep
    notoken="disp-lntok-$ts"   # expired lease but NO token label         -> keep (token guard)
    forged="forged-lease-$ts"  # expired lease + valid token, non-disp NAME -> keep (name guard)

    as_admin podman run -d --name "$exp" \
        --label qdistro_disposable=1 --label "qdistro_tier2_token=$tok_exp" \
        --label "qdistro_lease_ttl=1" --label "qdistro_lease_created=$past" \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail lease-sweep "could not create expired fixture $exp"
    as_admin podman run -d --name "$fresh" \
        --label qdistro_disposable=1 --label "qdistro_tier2_token=$tok_fresh" \
        --label "qdistro_lease_ttl=100000" --label "qdistro_lease_created=$now" \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail lease-sweep "could not create fresh fixture $fresh"
    as_admin podman run -d --name "$nolease" \
        --label qdistro_disposable=1 --label "qdistro_tier2_token=$tok_nolease" \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail lease-sweep "could not create no-lease fixture $nolease"
    as_admin podman run -d --name "$notoken" \
        --label qdistro_disposable=1 \
        --label "qdistro_lease_ttl=1" --label "qdistro_lease_created=$past" \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail lease-sweep "could not create no-token fixture $notoken"
    as_admin podman run -d --name "$forged" \
        --label qdistro_disposable=1 --label "qdistro_tier2_token=$tok_forged" \
        --label "qdistro_lease_ttl=1" --label "qdistro_lease_created=$past" \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail lease-sweep "could not create forged-name fixture $forged"

    local py_out
    py_out=$(XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        python3 - "$exp" "$fresh" "$nolease" "$notoken" "$forged" <<PY 2>&1
import sys, time
sys.path.insert(0, "$LIBEXEC")
import qdistro_session_manager as M
exp, fresh, nolease, notoken, forged = sys.argv[1:6]
ops = M._SystemOps()

# Real podman label read: every labelled fixture is enumerated; the candidate
# dict carries the raw label values (an absent label comes back as None, since
# disp_lease_candidates now parses `podman ps --format json` where an absent
# label is null -> None, not the old Go-template '<no value>'/'' sentinel).
cands = {c["name"]: c for c in ops.disp_lease_candidates()}
for n in (exp, fresh, nolease, notoken, forged):
    assert n in cands, f"{n!r} not enumerated by disp_lease_candidates: {list(cands)}"
# An absent label is None off the json read (older Go-template reads emitted ''
# or the literal '<no value>'); parse_lease_seconds maps all three to None ->
# survive. Accept every shape so the probe is robust across podman versions.
assert cands[nolease]["ttl"] in (None, "", "<no value>"), cands[nolease]
assert cands[notoken]["token"] in (None, "", "<no value>"), cands[notoken]
print("ENUM_OK")

# Drive the real store sweep (real dispose -> real podman rm). now() is live, so
# the ttl=1 created=now-100 fixtures are genuinely expired.
store = M._SiloStore(ops, config_path=M.Path("/tmp/lease-sweep-silos.yaml"))
reaped = store.sweep_expired_leases()
assert reaped == [exp], f"sweep reaped {reaped!r}, expected exactly [{exp!r}]"
print("SWEEP_OK")
PY
)
    echo "$py_out" >&2
    echo "$py_out" | grep -q ENUM_OK  || fail lease-sweep "disp_lease_candidates did not enumerate the fixtures / parse the absent (None) labels"
    echo "$py_out" | grep -q SWEEP_OK || fail lease-sweep "sweep_expired_leases did not reap EXACTLY the expired well-formed disposable"
    pass "lease sweep enumerated by label, reaped only the expired well-formed disposable"

    # Ground truth from podman: only the expired one is gone; every guard-skipped
    # fixture survived (fresh / no-lease / no-token / forged-name).
    as_admin podman container exists "$exp" 2>/dev/null \
        && fail lease-sweep "expired disposable $exp survived the sweep"
    local n
    for n in "$fresh" "$nolease" "$notoken" "$forged"; do
        as_admin podman container exists "$n" 2>/dev/null \
            || fail lease-sweep "guard-skipped fixture $n was wrongly reaped (MUST survive)"
    done
    pass "fresh / no-lease / no-token / forged-name fixtures all survived"

    for n in "$fresh" "$nolease" "$notoken" "$forged"; do
        as_admin podman rm -f "$n" >/dev/null 2>&1 || true
    done
    rm -f /tmp/lease-sweep-silos.yaml 2>/dev/null || true
    pass lease-sweep
}

cmd_proctree_spawn_labels() {
    # Prove the SHIPPED spawn stamps the process-tree + workflow lease labels on
    # the REAL container when their opt-in knobs are set (the spawn-side half of
    # the new predicates; the daemon-side reap is cmd_proctree_sweep, the
    # workflow teardown is cmd_workflow_dispose).
    clean_disp
    local out err container l_pt l_grace l_created l_wf
    out=$(mktemp); err=$(mktemp); container=""; SPAWN_PID=""
    # shellcheck disable=SC2317
    _psl_cleanup() {
        [ -n "${container:-}" ] && as_admin podman rm -f "$container" >/dev/null 2>&1
        [ -n "${SPAWN_PID:-}" ] && kill "$SPAWN_PID" 2>/dev/null
        rm -f "$out" "$err" 2>/dev/null
        return 0
    }
    trap _psl_cleanup EXIT

    as_admin env QDISTRO_DISPOSABLE_LEASE_PROCTREE=1 \
        QDISTRO_DISPOSABLE_LEASE_PROCTREE_GRACE=45 \
        QDISTRO_DISPOSABLE_WORKFLOW=wfstep-1 \
        "$SPAWN" --disposable "$WORKLOAD" -- weston-terminal \
        >"$out" 2>"$err" &
    SPAWN_PID=$!
    for _ in $(seq 1 60); do
        container=$(awk -F= '/^CONTAINER=/{print $2; exit}' "$out" 2>/dev/null)
        [ -n "$container" ] && as_admin podman container exists "$container" 2>/dev/null && break
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 0.5
    done
    [ -n "$container" ] && as_admin podman container exists "$container" 2>/dev/null \
        || { echo "--- spawn stderr ---" >&2; cat "$err" >&2; \
             fail proctree-spawn-labels "disposable container never appeared"; }

    l_pt=$(as_admin podman inspect --format \
        '{{index .Config.Labels "qdistro_lease_proctree"}}' "$container" 2>/dev/null)
    [ "$l_pt" = "1" ] \
        || fail proctree-spawn-labels "qdistro_lease_proctree label '$l_pt' != 1"
    pass "shipped spawn stamped qdistro_lease_proctree=1"
    l_grace=$(as_admin podman inspect --format \
        '{{index .Config.Labels "qdistro_lease_proctree_grace"}}' "$container" 2>/dev/null)
    [ "$l_grace" = "45" ] \
        || fail proctree-spawn-labels "qdistro_lease_proctree_grace label '$l_grace' != 45"
    pass "shipped spawn stamped qdistro_lease_proctree_grace=45"
    # The created anchor must be stamped because proctree was opted in (even with
    # NO TTL) — it is the shared age anchor for the grace window.
    l_created=$(as_admin podman inspect --format \
        '{{index .Config.Labels "qdistro_lease_created"}}' "$container" 2>/dev/null)
    [[ "$l_created" =~ ^[0-9]+$ ]] \
        || fail proctree-spawn-labels "qdistro_lease_created '$l_created' not stamped for a proctree-only lease"
    pass "shipped spawn stamped qdistro_lease_created (shared anchor) without a TTL"
    l_wf=$(as_admin podman inspect --format \
        '{{index .Config.Labels "qdistro_lease_workflow"}}' "$container" 2>/dev/null)
    [ "$l_wf" = "wfstep-1" ] \
        || fail proctree-spawn-labels "qdistro_lease_workflow label '$l_wf' != wfstep-1"
    pass "shipped spawn stamped qdistro_lease_workflow=wfstep-1"

    as_admin podman rm -f "$container" >/dev/null 2>&1 || true
    container=""
    pass proctree-spawn-labels
}

cmd_proctree_sweep() {
    # Drive the REAL daemon-side process-tree-empty machinery against real
    # podman: _SystemOps.disp_proctree_candidates (label read) +
    # disp_container_top_pids (real `podman top`) + proctree_empty +
    # _SiloStore.sweep_empty_proctrees -> dispose() -> real `podman rm -f`.
    #
    # The fixtures are plain `podman run` containers whose PID1 is the image's
    # weston entrypoint:
    #   empty   = ONLY weston (PID1) running        -> REAP (past grace)
    #   busy    = weston + a child sleep            -> keep (tree not empty)
    #   fresh   = empty tree but created NOW        -> keep (within grace)
    #   noopt   = empty tree but no proctree label  -> keep (not opted in)
    #   notok   = empty tree, opted in, NO token    -> keep (token guard)
    clean_disp
    local ts now past tok_e tok_b tok_f tok_n
    ts=$(date +%Y%m%d-%H%M%S)
    now=$(date +%s); past=$(( now - 1000 ))
    tok_e=$(gen_token); tok_b=$(gen_token); tok_f=$(gen_token); tok_n=$(gen_token)

    local empty busy fresh noopt notok
    empty="disp-ptempty-$ts"
    busy="disp-ptbusy-$ts"
    fresh="disp-ptfresh-$ts"
    noopt="disp-ptnoopt-$ts"
    notok="disp-ptntok-$ts"

    # empty = a single, STABLE PID1 (sleep) and nothing else. We use `sleep`
    # rather than a real weston PID1 because a headless weston with no backend
    # flaps (exits/respawns), which would make the end-to-end real-`rm` sweep
    # flaky. The end-to-end sweep below therefore asserts PID1-ONLY detection +
    # the full reap path with pid1_comm overridden to this fixture's `sleep`; the
    # PRODUCTION pid1_comm="weston" branch is proven SEPARATELY and HONESTLY by
    # the real-weston-PID1 assertion further down (no monkeypatch there).
    as_admin podman run -d --name "$empty" \
        --label qdistro_disposable=1 --label "qdistro_tier2_token=$tok_e" \
        --label qdistro_lease_proctree=1 --label "qdistro_lease_created=$past" \
        --label qdistro_lease_proctree_grace=30 \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail proctree-sweep "could not create empty fixture $empty"

    # busy = PID1 + a child, so the tree is never "PID1 only".
    as_admin podman run -d --name "$busy" \
        --label qdistro_disposable=1 --label "qdistro_tier2_token=$tok_b" \
        --label qdistro_lease_proctree=1 --label "qdistro_lease_created=$past" \
        --entrypoint sh "$IMAGE" -c 'sleep 600 & sleep 600' >/dev/null 2>&1 \
        || fail proctree-sweep "could not create busy fixture $busy"
    # fresh = PID1-only but created NOW (within grace) -> survives.
    as_admin podman run -d --name "$fresh" \
        --label qdistro_disposable=1 --label "qdistro_tier2_token=$tok_f" \
        --label qdistro_lease_proctree=1 --label "qdistro_lease_created=$now" \
        --label qdistro_lease_proctree_grace=100000 \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail proctree-sweep "could not create fresh fixture $fresh"
    # noopt = PID1-only, past created, but NOT opted in -> never enumerated.
    as_admin podman run -d --name "$noopt" \
        --label qdistro_disposable=1 --label "qdistro_tier2_token=$tok_n" \
        --label "qdistro_lease_created=$past" \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail proctree-sweep "could not create no-opt fixture $noopt"
    # notok = opted in, past created, PID1-only, but NO token label -> guard skip.
    as_admin podman run -d --name "$notok" \
        --label qdistro_disposable=1 \
        --label qdistro_lease_proctree=1 --label "qdistro_lease_created=$past" \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail proctree-sweep "could not create no-token fixture $notok"

    local py_out
    py_out=$(XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        python3 - "$empty" "$busy" "$fresh" "$noopt" "$notok" <<PY 2>&1
import sys
sys.path.insert(0, "$LIBEXEC")
import qdistro_session_manager as M
import qdistro_disposables as D
empty, busy, fresh, noopt, notok = sys.argv[1:6]
ops = M._SystemOps()

# Real label read: opted-in fixtures are enumerated; noopt is NOT (the
# qdistro_lease_proctree=1 --filter excludes it), proving the opt-in gate.
cands = {c["name"]: c for c in ops.disp_proctree_candidates()}
assert empty in cands and busy in cands and fresh in cands and notok in cands, \
    f"opted-in fixtures missing from proctree candidates: {list(cands)}"
assert noopt not in cands, f"{noopt!r} (not opted in) was wrongly enumerated"
print("ENUM_OK")

# Real podman top: the empty fixture's tree is PID1-only; the busy fixture has a
# child. These fixtures' PID1 comm is the entrypoint ('sleep'/'sh'), not weston,
# so we assert proctree_empty against the ACTUAL entrypoint comm to prove the
# PID1-ONLY discrimination honestly on real podman output.
top_e = ops.disp_container_top_pids(empty)
top_b = ops.disp_container_top_pids(busy)
assert top_e and top_b, "podman top returned nothing"
# empty: exactly one process row, PID1 = sleep.
assert D.proctree_empty(top_e, pid1_comm="sleep"), f"empty tree not detected: {top_e!r}"
# busy: more than one process -> NOT empty under any pid1_comm.
assert not D.proctree_empty(top_b, pid1_comm="sh"), f"busy tree wrongly empty: {top_b!r}"
print("TOP_OK")

# Drive the REAL store sweep end to end. The shipped predicate uses pid1_comm=
# 'weston'; our fixtures' PID1 is 'sleep'/'sh', so the shipped sweep would find
# NONE empty (PID1 comm mismatch is fail-closed). To exercise the full reap path
# (eligibility -> top -> proctree_empty -> dispose -> real rm) we monkeypatch the
# expected PID1 comm to 'sleep' for THIS run only, matching the empty fixture.
import qdistro_session_manager as _M
_orig = D.proctree_empty
D.proctree_empty = lambda out, pid1_comm=D.PROCTREE_PID1_COMM: _orig(out, pid1_comm="sleep")
try:
    store = _M._SiloStore(ops, config_path=_M.Path("/tmp/proctree-sweep-silos.yaml"))
    reaped = store.sweep_empty_proctrees()
finally:
    D.proctree_empty = _orig
assert reaped == [empty], f"proctree sweep reaped {reaped!r}, expected exactly [{empty!r}]"
print("SWEEP_OK")
PY
)
    echo "$py_out" >&2
    echo "$py_out" | grep -q ENUM_OK  || fail proctree-sweep "disp_proctree_candidates did not enumerate opted-in fixtures / excluded the non-opted-in one"
    echo "$py_out" | grep -q TOP_OK   || fail proctree-sweep "real podman top / proctree_empty did not discriminate PID1-only from a busy tree"
    echo "$py_out" | grep -q SWEEP_OK || fail proctree-sweep "sweep_empty_proctrees did not reap EXACTLY the empty-tree disposable"
    pass "proctree sweep: real label read + real podman top discriminated PID1-only, reaped only the empty-tree disposable"

    # Ground truth: only the empty-tree one is gone; every guard-skipped fixture
    # (busy / within-grace / not-opted-in / no-token) survived.
    as_admin podman container exists "$empty" 2>/dev/null \
        && fail proctree-sweep "empty-tree disposable $empty survived the sweep"
    local n
    for n in "$busy" "$fresh" "$noopt" "$notok"; do
        as_admin podman container exists "$n" 2>/dev/null \
            || fail proctree-sweep "guard-skipped fixture $n was wrongly reaped (MUST survive)"
    done
    pass "busy / within-grace / not-opted-in / no-token fixtures all survived"

    for n in "$busy" "$fresh" "$noopt" "$notok"; do
        as_admin podman rm -f "$n" >/dev/null 2>&1 || true
    done

    # PRODUCTION-BRANCH proof (codex code-review MINOR 1): the sweep above
    # overrode pid1_comm to the fixtures' `sleep`; here we prove the SHIPPED
    # default pid1_comm="weston" matches a REAL weston PID1 on real `podman top`
    # output — no monkeypatch. A headless weston with no backend may flap, so we
    # only assert the comm match WHILE the container is alive (if it has already
    # exited, --rm/podman makes top fail and we skip rather than false-fail).
    local west tok_w
    west="disp-ptweston-$ts"; tok_w=$(gen_token)
    if as_admin podman run -d --name "$west" \
        --label qdistro_disposable=1 --label "qdistro_tier2_token=$tok_w" \
        --entrypoint weston "$IMAGE" --backend=headless-backend.so \
        >/dev/null 2>&1; then
        sleep 1
        local west_out
        west_out=$(XDG_RUNTIME_DIR="$RUNTIME_DIR" \
            python3 - "$west" <<'PY' 2>&1
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
import qdistro_session_manager as M
import qdistro_disposables as D
west = sys.argv[1]
ops = M._SystemOps()
top = ops.disp_container_top_pids(west)
if top is None:
    print("WESTON_GONE")          # weston flapped/exited; skip, do not false-fail
else:
    rows = D.parse_podman_top_pids(top)
    # If weston is genuinely the sole PID1, the SHIPPED default predicate fires.
    if rows is not None and len(rows) == 1 and rows[0][0] == 1:
        assert D.proctree_empty(top), (
            f"production proctree_empty(default weston) did NOT match a real "
            f"weston PID1: {top!r}")
        print("WESTON_PROD_OK")
    else:
        # weston spawned helpers / a child — still proves real top parsing, and
        # the default predicate must correctly NOT fire on a multi-process tree.
        assert not D.proctree_empty(top), (
            f"production predicate wrongly fired on a multi-process weston "
            f"tree: {top!r}")
        print("WESTON_MULTI_OK")
PY
)
        echo "$west_out" >&2
        if echo "$west_out" | grep -q WESTON_PROD_OK; then
            pass "production pid1_comm=weston branch matched a REAL weston PID1 on real podman top (no monkeypatch)"
        elif echo "$west_out" | grep -q WESTON_MULTI_OK; then
            pass "production predicate correctly did NOT fire on a real multi-process weston tree"
        elif echo "$west_out" | grep -q WESTON_GONE; then
            echo "NOTE: real weston fixture exited before inspection (headless flap); production-branch assertion skipped this run" >&2
            pass "production-branch real-weston check skipped (weston flapped); unit tests remain the weston-comm oracle"
        else
            fail proctree-sweep "real-weston production-branch check errored: $west_out"
        fi
        as_admin podman rm -f "$west" >/dev/null 2>&1 || true
    else
        echo "NOTE: could not start a real weston PID1 fixture (headless backend absent); production-branch assertion skipped" >&2
        pass "production-branch real-weston check skipped (no headless weston); unit tests remain the weston-comm oracle"
    fi

    rm -f /tmp/proctree-sweep-silos.yaml 2>/dev/null || true
    pass proctree-sweep
}

cmd_workflow_dispose() {
    # Drive the REAL DisposeByWorkflow teardown against real podman:
    # _SystemOps.disp_containers_by_workflow (dual-label filter) +
    # _SiloStore.dispose_by_workflow -> dispose() -> real `podman rm -f`. A
    # workflow step that spawned SEVERAL disposables tears them ALL down with one
    # call keyed on the shared qdistro_lease_workflow=<id> label.
    clean_disp
    local ts wf other_wf tok1 tok2 tok3
    ts=$(date +%Y%m%d-%H%M%S)
    wf="wfgroup-$ts"; other_wf="wfother-$ts"
    tok1=$(gen_token); tok2=$(gen_token); tok3=$(gen_token)

    local a b c
    a="disp-wfa-$ts"   # in the group -> torn down
    b="disp-wfb-$ts"   # in the group -> torn down
    c="disp-wfc-$ts"   # DIFFERENT workflow id -> survives

    as_admin podman run -d --name "$a" \
        --label qdistro_disposable=1 --label "qdistro_tier2_token=$tok1" \
        --label "qdistro_lease_workflow=$wf" \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail workflow-dispose "could not create fixture $a"
    as_admin podman run -d --name "$b" \
        --label qdistro_disposable=1 --label "qdistro_tier2_token=$tok2" \
        --label "qdistro_lease_workflow=$wf" \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail workflow-dispose "could not create fixture $b"
    as_admin podman run -d --name "$c" \
        --label qdistro_disposable=1 --label "qdistro_tier2_token=$tok3" \
        --label "qdistro_lease_workflow=$other_wf" \
        --entrypoint sleep "$IMAGE" 600 >/dev/null 2>&1 \
        || fail workflow-dispose "could not create fixture $c"

    local py_out
    py_out=$(XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        python3 - "$wf" "$a" "$b" <<PY 2>&1
import sys
sys.path.insert(0, "$LIBEXEC")
import qdistro_session_manager as M
wf, a, b = sys.argv[1:4]
ops = M._SystemOps()

# Dual-label filter resolves EXACTLY the two group members (the other-workflow
# container is excluded by the workflow id, and only qdistro_disposable=1
# containers are ever considered).
names = sorted(ops.disp_containers_by_workflow(wf))
assert names == sorted([a, b]), f"by-workflow resolved {names!r}, expected {sorted([a,b])!r}"
print("RESOLVE_OK")

store = M._SiloStore(ops, config_path=M.Path("/tmp/wf-dispose-silos.yaml"))
n = store.dispose_by_workflow(wf)
assert n == 2, f"dispose_by_workflow reaped {n}, expected 2"
print("DISPOSE_OK")

# Malformed id never reaches a filter -> BadArgument.
try:
    store.dispose_by_workflow("Bad Id!")
    print("BADARG_FAIL")
except M.BadArgument:
    print("BADARG_OK")

# A re-run with everything gone is idempotent 0 (no live disposables carry it).
assert store.dispose_by_workflow(wf) == 0
print("IDEMPOTENT_OK")
PY
)
    echo "$py_out" >&2
    echo "$py_out" | grep -q RESOLVE_OK    || fail workflow-dispose "disp_containers_by_workflow did not resolve exactly the group via the dual-label filter"
    echo "$py_out" | grep -q DISPOSE_OK    || fail workflow-dispose "dispose_by_workflow did not tear down both group members"
    echo "$py_out" | grep -q BADARG_OK     || fail workflow-dispose "a malformed workflow id was not rejected fail-closed (BadArgument)"
    echo "$py_out" | grep -q IDEMPOTENT_OK || fail workflow-dispose "dispose_by_workflow re-run was not idempotent 0"
    pass "workflow dispose: dual-label resolve, group torn down, malformed id rejected, idempotent re-run"

    # Ground truth: both group members gone; the other-workflow container survived.
    local n
    for n in "$a" "$b"; do
        as_admin podman container exists "$n" 2>/dev/null \
            && fail workflow-dispose "group member $n survived DisposeByWorkflow"
    done
    as_admin podman container exists "$c" 2>/dev/null \
        || fail workflow-dispose "the other-workflow container $c was wrongly torn down"
    pass "both group members torn down; the other-workflow disposable survived"

    as_admin podman rm -f "$c" >/dev/null 2>&1 || true
    rm -f /tmp/wf-dispose-silos.yaml 2>/dev/null || true
    pass workflow-dispose
}

cmd_teardown() {
    clean_disp
    rm -f "$RULE_FILE" 2>/dev/null || true
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    as_admin podman rmi "$DENY_IMAGE" >/dev/null 2>&1 || true
    rm -rf "$TIER2_BUILD_DIR" /tmp/disp-build.log 2>/dev/null || true
    rm -f /tmp/lease-sweep-silos.yaml /tmp/proctree-sweep-silos.yaml \
          /tmp/wf-dispose-silos.yaml 2>/dev/null || true
    pass teardown
}

case "${1:-}" in
    setup) cmd_setup ;;
    spawn-window-close) cmd_spawn_window_close ;;
    reaper-sweep) cmd_reaper_sweep ;;
    deny-fail-closed) cmd_deny_fail_closed ;;
    lease-spawn-labels) cmd_lease_spawn_labels ;;
    lease-sweep) cmd_lease_sweep ;;
    proctree-spawn-labels) cmd_proctree_spawn_labels ;;
    proctree-sweep) cmd_proctree_sweep ;;
    workflow-dispose) cmd_workflow_dispose ;;
    teardown) cmd_teardown ;;
    *) echo "usage: $0 {setup|spawn-window-close|reaper-sweep|deny-fail-closed|lease-spawn-labels|lease-sweep|proctree-spawn-labels|proctree-sweep|workflow-dispose|teardown}" >&2; exit 2 ;;
esac
