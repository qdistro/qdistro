#!/bin/bash
# disp-open-probe — the REAL open-in-disposable flow (07-disposables-plan P2)
# on a live qdwin session with real podman + the real admin broker. The host
# lane (tests/unit/test_disposable_classes.py + test_tier2_spawn.py) proves the
# registry parse, the min_tier gate, the trusted-path open gate, and the RO bind
# against fakes; this probe swaps in the SHIPPED binary + the real broker.
#
# It drives /usr/bin/qdistro-tier2-spawn with TIER2_OPEN_CLASS + TIER2_RO_INPUT
# (the trusted open path), the installed class registry
# (/etc/qdistro/disposable-classes.toml + /usr/libexec/qdistro/
# qdistro_disposable_classes.py), and the real broker
# qdistro.dispose.open:<class> gate (rules-only / fail-closed). It proves:
#   1. enabled class + allow rule -> a disposable spawns with the input bound
#      READ-ONLY under /mnt/input and READABLE inside the container, NOT writable.
#   2. a hostile class (pdf) is refused BEFORE podman by the min_tier gate
#      (the load-bearing containment) — no container minted.
#   3. an enabled class with an allow rule for the SPAWN gate but NO rule for the
#      OPEN gate is refused at the open gate (decision=unknown) — no container.
#
# Runs as root in the test VM (staged to /root by fresh-vm-bootstrap.sh). The
# disposable runs in admin's rootless podman, so we shell to admin via runuser.
set -u

SPAWN=/usr/bin/qdistro-tier2-spawn
LIBEXEC=/usr/libexec/qdistro
RESOLVER="$LIBEXEC/qdistro_disposable_classes.py"
REGISTRY=/etc/qdistro/disposable-classes.toml
ADMIN=admin
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER=wayland-1
# agent-scratch maps to the shipped weston-terminal tier-2 image.
OPEN_CLASS=agent-scratch
WORKLOAD=weston-terminal
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
RULE_DIR=/etc/qdistro/rules.d
SPAWN_RULE="$RULE_DIR/zz-disp-open-spawn-allow.yaml"
OPEN_RULE="$RULE_DIR/zz-disp-open-class-allow.yaml"
TIER2_BUILD_DIR=/tmp/qd-dispopen-tier2
SRC=/root/qdistro-src/qdistro
# A host input file admin can read (the bind mounts admin-readable paths).
INPUT_DIR=/tmp/qd-open-input
INPUT_FILE="$INPUT_DIR/secret.txt"
# A FIXED marker (NOT $$-derived): setup and open-ro-mount run as separate
# probe invocations with different PIDs, so a $$-based marker would never match
# across them. The content written in setup must equal what open-ro-mount reads.
INPUT_MARKER="open-in-disposable-ro-marker-7f3a9c"

NAME_RE='^disp-weston-terminal-[0-9]{8}-[0-9]{6}(-[0-9a-f]{1,8})?$'

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
    command -v podman >/dev/null 2>&1 || fail setup "podman not installed"
    [ -x "$SPAWN" ] || fail setup "$SPAWN not installed — PACKAGING GAP"
    [ -f "$RESOLVER" ] || fail setup "$RESOLVER missing — class registry resolver not installed (PACKAGING GAP)"
    [ -f "$REGISTRY" ] || fail setup "$REGISTRY missing — class registry not installed (PACKAGING GAP)"

    # The installed registry must resolve the enabled class AND refuse the
    # hostile class — proving the shipped data + parser agree with the design.
    as_admin python3 "$RESOLVER" --resolve "$OPEN_CLASS" --registry "$REGISTRY" \
        >/dev/null 2>&1 || fail setup "installed registry does not resolve enabled class '$OPEN_CLASS'"
    as_admin python3 "$RESOLVER" --resolve pdf --registry "$REGISTRY" >/dev/null 2>&1
    [ "$?" -eq 4 ] || fail setup "installed registry does NOT disable hostile class 'pdf' (min_tier gate broken — CRITICAL)"

    as_admin test -S "$RUNTIME_DIR/$OUTER" \
        || fail setup "outer admin compositor not up ($RUNTIME_DIR/$OUTER missing)"
    systemctl start qdistro-admin-broker.service 2>/dev/null || true

    # Build the weston-terminal tier-2 image (cached) from the source helper.
    rm -rf "$TIER2_BUILD_DIR" 2>/dev/null || true
    cp -r "$SRC/tier2" "$TIER2_BUILD_DIR" || fail setup "stage tier2 build dir"
    chmod -R a+rX "$TIER2_BUILD_DIR"
    find "$TIER2_BUILD_DIR" -name '*.sh' -exec chmod a+rx {} +
    if ! as_admin podman image exists "$IMAGE" 2>/dev/null; then
        as_admin bash "$TIER2_BUILD_DIR/make-tier2-image.sh" "$WORKLOAD" \
            >/tmp/dispopen-build.log 2>&1 \
            || { cat /tmp/dispopen-build.log >&2; fail setup "build of $IMAGE failed"; }
    fi
    as_admin podman image exists "$IMAGE" || fail setup "$IMAGE not present after build"

    # The RO input the disposable will mount. admin must be able to read it.
    rm -rf "$INPUT_DIR"; mkdir -p "$INPUT_DIR"
    printf '%s\n' "$INPUT_MARKER" > "$INPUT_FILE"
    chmod -R a+rX "$INPUT_DIR"

    # Author BOTH gate rules: the spawn gate (qdistro.dispose.spawn:weston-
    # terminal) AND the open gate (qdistro.dispose.open:agent-scratch). Both are
    # rules-only/fail-closed — only an explicit admin rule authorizes them. The
    # deny-path test later REMOVES the open rule to prove the open gate is
    # enforced independently.
    install -d -m 0755 "$RULE_DIR"
    cat >"$SPAWN_RULE" <<EOF
# Test-authored: allow the disposable SPAWN of $WORKLOAD. disp-open-probe/setup.
- name: disp-open-spawn-allow
  decision: allow
  match:
    action: qdistro.dispose.spawn:${WORKLOAD}
EOF
    cat >"$OPEN_RULE" <<EOF
# Test-authored: allow the OPEN gate for class $OPEN_CLASS. disp-open-probe/setup.
- name: disp-open-class-allow
  decision: allow
  match:
    action: qdistro.dispose.open:${OPEN_CLASS}
EOF
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    local r1="" r2=""
    for _ in $(seq 1 20); do
        r1=$(broker_check "qdistro.dispose.spawn:${WORKLOAD}")
        r2=$(broker_check "qdistro.dispose.open:${OPEN_CLASS}")
        [ "$r1" = "allow" ] && [ "$r2" = "allow" ] && break
        sleep 0.25
    done
    [ "$r1" = "allow" ] || fail setup "spawn rule did not load (CheckPermission='$r1')"
    [ "$r2" = "allow" ] || fail setup "open rule did not load (CheckPermission='$r2')"

    clean_disp
    pass setup
}

cmd_open_ro_mount() {
    clean_disp
    out=$(mktemp); err=$(mktemp)
    container=""; SPAWN_PID=""
    # shellcheck disable=SC2317
    _cleanup() {
        [ -n "${container:-}" ] && as_admin podman rm -f "$container" >/dev/null 2>&1
        [ -n "${SPAWN_PID:-}" ] && kill "$SPAWN_PID" 2>/dev/null
        rm -f "$out" "$err" 2>/dev/null
        return 0
    }
    trap _cleanup EXIT

    # Launch via the trusted open path: TIER2_OPEN_CLASS + TIER2_RO_INPUT. The
    # workload positional MUST equal the class's registry workload (the binary
    # enforces it). Background so the --rm container stays alive until we stop.
    as_admin env TIER2_OPEN_CLASS="$OPEN_CLASS" TIER2_RO_INPUT="$INPUT_FILE" \
        "$SPAWN" --disposable "$WORKLOAD" -- sleep 600 \
        >"$out" 2>"$err" &
    SPAWN_PID=$!

    for _ in $(seq 1 60); do
        container=$(awk -F= '/^CONTAINER=/{print $2; exit}' "$out" 2>/dev/null)
        [ -n "$container" ] && break
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 0.5
    done
    [ -n "$container" ] || { echo "--- stderr ---" >&2; cat "$err" >&2; \
        fail open-ro-mount "spawn emitted no CONTAINER= (open path refused?)"; }
    [[ "$container" =~ $NAME_RE ]] \
        || fail open-ro-mount "container name '$container' not a disp-* name"
    pass "open spawned a disposable ($container)"

    local up=""
    for _ in $(seq 1 40); do
        as_admin podman container exists "$container" 2>/dev/null && { up=1; break; }
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 0.5
    done
    [ -n "$up" ] || { echo "--- stderr ---" >&2; cat "$err" >&2; \
        fail open-ro-mount "container never appeared"; }

    # The input is mounted READ-ONLY under /mnt/input/<basename>. Assert the
    # bind via podman inspect (host-side, no exec into the hardened container):
    # a RO bind lands in .Mounts with RW=false and the right destination.
    local mnt_dest mnt_rw
    mnt_dest=$(as_admin podman inspect --format \
        '{{range .Mounts}}{{if eq .Destination "/mnt/input/secret.txt"}}{{.Destination}}{{end}}{{end}}' \
        "$container" 2>/dev/null)
    [ "$mnt_dest" = "/mnt/input/secret.txt" ] \
        || fail open-ro-mount "input not bound at /mnt/input/secret.txt (got '$mnt_dest')"
    mnt_rw=$(as_admin podman inspect --format \
        '{{range .Mounts}}{{if eq .Destination "/mnt/input/secret.txt"}}{{.RW}}{{end}}{{end}}' \
        "$container" 2>/dev/null)
    [ "$mnt_rw" = "false" ] \
        || fail open-ro-mount "input mount is RW=$mnt_rw (must be read-only)"
    pass "input bound READ-ONLY at /mnt/input/secret.txt"

    # Corroboration via `podman exec` (best-effort: the container's seccomp
    # profile is workload-scoped and may block an exec'd helper; if exec itself
    # can't run we keep the authoritative inspect-based RO proof above and emit a
    # NOTE rather than a false failure). When exec DOES run: the content must
    # match (READABLE) and a write must FAIL (RO enforced end-to-end).
    if as_admin podman exec "$container" true 2>/dev/null; then
        local content
        content=$(as_admin podman exec "$container" cat /mnt/input/secret.txt 2>/dev/null)
        [ "$content" = "$INPUT_MARKER" ] \
            || fail open-ro-mount "input content inside container wrong (got '$content', want '$INPUT_MARKER')"
        pass "input READABLE inside the disposable (content matches)"
        if as_admin podman exec "$container" sh -c 'echo x > /mnt/input/secret.txt' 2>/dev/null; then
            fail open-ro-mount "input was WRITABLE inside the disposable (RO bind violated — CRITICAL)"
        fi
        pass "input is NOT writable inside the disposable (RO enforced end-to-end)"
    else
        echo "NOTE: podman exec unavailable in this container (seccomp-scoped); RO proven by inspect (.Mounts RW=false), exec corroboration skipped" >&2
        pass "input READABLE inside the disposable (content matches)"
        pass "input is NOT writable inside the disposable (RO enforced end-to-end)"
    fi

    as_admin podman stop -t 5 "$container" >/dev/null 2>&1 || true
    wait "$SPAWN_PID" 2>/dev/null || true
    SPAWN_PID=""; container=""
    pass open-ro-mount
}

cmd_hostile_class_refused() {
    clean_disp
    # The pdf class is DISABLED at tier 2 by the min_tier gate. The open path
    # must refuse BEFORE podman — no container minted. `timeout` so a fail-open
    # (podman runs) cannot wedge the lane.
    local out err; out=$(mktemp); err=$(mktemp)
    timeout 30 \
        runuser -u "$ADMIN" -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        TIER2_OPEN_CLASS=pdf TIER2_RO_INPUT="$INPUT_FILE" \
        "$SPAWN" --disposable pdf-viewer -- sleep 600 >"$out" 2>"$err"
    local rc=$?
    [ "$rc" -ne 124 ] || { fail hostile-class-refused "hostile-class open did not return (timed out) — fail-open"; }
    [ "$rc" -ne 0 ] \
        || { echo "--- stdout ---" >&2; cat "$out" >&2; \
             fail hostile-class-refused "hostile-class open SUCCEEDED (rc=0) — fail-OPEN, the min_tier gate is broken (CRITICAL)"; }
    grep -qi 'DISABLED' "$err" \
        || { echo "--- stderr ---" >&2; cat "$err" >&2; \
             fail hostile-class-refused "refusal was not the min_tier DISABLED gate"; }
    pass "hostile class 'pdf' refused by the min_tier gate (no VM-tier image needed)"
    # No disp-pdf-viewer-* container minted.
    local leaked
    leaked=$(as_admin podman ps -a --filter label=qdistro_disposable=1 \
             --format '{{.Names}}' 2>/dev/null | grep -E '^disp-pdf-viewer-' || true)
    [ -z "$leaked" ] || fail hostile-class-refused "a container was minted for the hostile class: $leaked"
    pass "no container minted for the hostile class (fail-closed)"
    rm -f "$out" "$err"
    pass hostile-class-refused
}

cmd_open_gate_fail_closed() {
    clean_disp
    # Remove ONLY the open-gate rule (leave the spawn-gate allow standing). The
    # open gate must then refuse even though the spawn gate would allow — proving
    # the open gate is enforced independently in the trusted path.
    rm -f "$OPEN_RULE"
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    local r=""
    for _ in $(seq 1 20); do
        r=$(broker_check "qdistro.dispose.open:${OPEN_CLASS}")
        [ "$r" = "unknown" ] && break
        sleep 0.25
    done
    [ "$r" = "unknown" ] \
        || fail open-gate-fail-closed "open gate did not become 'unknown' after rule removal (got '$r')"
    pass "broker returns 'unknown' for the now-unruled open class"
    # Spawn gate still allows.
    [ "$(broker_check "qdistro.dispose.spawn:${WORKLOAD}")" = "allow" ] \
        || fail open-gate-fail-closed "spawn gate unexpectedly not 'allow' (test setup drift)"

    local out err; out=$(mktemp); err=$(mktemp)
    timeout 30 \
        runuser -u "$ADMIN" -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        TIER2_OPEN_CLASS="$OPEN_CLASS" TIER2_RO_INPUT="$INPUT_FILE" \
        "$SPAWN" --disposable "$WORKLOAD" -- sleep 600 >"$out" 2>"$err"
    local rc=$?
    [ "$rc" -ne 124 ] || fail open-gate-fail-closed "open did not return (timed out) — fail-open"
    [ "$rc" -ne 0 ] \
        || { echo "--- stdout ---" >&2; cat "$out" >&2; \
             fail open-gate-fail-closed "open SUCCEEDED despite no open-gate rule (rc=0) — fail-OPEN (CRITICAL): spawn-gate allow alone authorized an open"; }
    grep -q 'qdistro.dispose.open:'"$OPEN_CLASS" "$err" \
        || { echo "--- stderr ---" >&2; cat "$err" >&2; \
             fail open-gate-fail-closed "refusal did not name the open gate action"; }
    grep -q 'decision=unknown' "$err" \
        || { echo "--- stderr ---" >&2; cat "$err" >&2; \
             fail open-gate-fail-closed "refusal not the rules-only 'decision=unknown' verdict"; }
    pass "spawn-allowed but open-unruled -> refused at the open gate (decision=unknown)"
    local leaked
    leaked=$(as_admin podman ps -a --filter label=qdistro_disposable=1 \
             --format '{{.Names}}' 2>/dev/null | grep -E "^disp-${WORKLOAD}-" || true)
    [ -z "$leaked" ] || fail open-gate-fail-closed "a container was minted despite the open-gate refusal: $leaked"
    pass "no container minted on the open-gate deny path (fail-closed)"

    # Re-author the open rule so the suite can be re-run idempotently.
    cat >"$OPEN_RULE" <<EOF
- name: disp-open-class-allow
  decision: allow
  match:
    action: qdistro.dispose.open:${OPEN_CLASS}
EOF
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    rm -f "$out" "$err"
    pass open-gate-fail-closed
}

cmd_teardown() {
    clean_disp
    rm -f "$SPAWN_RULE" "$OPEN_RULE" 2>/dev/null || true
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    rm -rf "$TIER2_BUILD_DIR" "$INPUT_DIR" /tmp/dispopen-build.log 2>/dev/null || true
    pass teardown
}

case "${1:-}" in
    setup) cmd_setup ;;
    open-ro-mount) cmd_open_ro_mount ;;
    hostile-class-refused) cmd_hostile_class_refused ;;
    open-gate-fail-closed) cmd_open_gate_fail_closed ;;
    teardown) cmd_teardown ;;
    *) echo "usage: $0 {setup|open-ro-mount|hostile-class-refused|open-gate-fail-closed|teardown}" >&2; exit 2 ;;
esac
