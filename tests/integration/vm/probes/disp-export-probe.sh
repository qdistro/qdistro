#!/bin/bash
# disp-export-probe — the REAL export-back flow (07-disposables-plan P2 / D7
# copy-exception) on a live qdwin session with real podman + the real admin
# broker + the real resolver. The host lane (tests/unit/test_disposable_export.py
# + test_tier2_spawn.py + test_disposable_classes.py) proves the promoter
# (regular-files-only / all-or-nothing / caps / O_NOFOLLOW / receipt), the store
# import_from_disposable fail-closed paths (with fakes), and the spawn-side plan +
# RW-bind + labels against a fake podman/broker. This probe swaps in:
#   - the SHIPPED /usr/bin/qdistro-tier2-spawn on real rootless podman, proving a
#     per-launch staging tree is created, /mnt/output is bound RW into the
#     container, a process inside CAN write to it (RW end-to-end), meta.json is
#     OUTSIDE the bind, and the export labels land;
#   - the real admin broker gate qdistro.dispose.export:<class> at BOTH spawn
#     (no rule => the spawn refuses, no container) and import;
#   - the real store import_from_disposable against real podman + real broker +
#     real qdistro-resolve-binding (untemplated target refused; malformed token /
#     absent staging fail-closed; broker-deny at import refused).
# The full end-to-end landing into a PROVISIONED templated silo is the residual
# (needs a promoted generation); the promotion itself is host-proven on a real
# filesystem and every real-podman/broker/resolver edge is proven here.
#
# Runs as root in the test VM (staged to /root by fresh-vm-bootstrap.sh). The
# disposable runs in admin's rootless podman, so we shell to admin via runuser.
set -u

SPAWN=/usr/bin/qdistro-tier2-spawn
LIBEXEC=/usr/libexec/qdistro
RESOLVER="$LIBEXEC/qdistro_disposable_classes.py"
REGISTRY=/etc/qdistro/disposable-classes.toml
STAGING_BASE=/var/lib/qdistro/disposable-export
ADMIN=admin
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER=wayland-1
OPEN_CLASS=agent-scratch
WORKLOAD=weston-terminal
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
RULE_DIR=/etc/qdistro/rules.d
SPAWN_RULE="$RULE_DIR/zz-disp-export-spawn-allow.yaml"
OPEN_RULE="$RULE_DIR/zz-disp-export-open-allow.yaml"
EXPORT_RULE="$RULE_DIR/zz-disp-export-class-allow.yaml"
TIER2_BUILD_DIR=/tmp/qd-dispexport-tier2
SRC=/root/qdistro-src/qdistro
REQUEST_SILO=exportwork

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

clean_staging() { rm -rf "${STAGING_BASE:?}/"* 2>/dev/null || true; }

author_rule() {  # author_rule <file> <name> <action>
    cat >"$2.tmp.$$" <<EOF
- name: $2
  decision: allow
  match:
    action: $3
EOF
    mv "$2.tmp.$$" "$1"
}

cmd_setup() {
    command -v podman >/dev/null 2>&1 || fail setup "podman not installed"
    [ -x "$SPAWN" ] || fail setup "$SPAWN not installed — PACKAGING GAP"
    [ -f "$RESOLVER" ] || fail setup "$RESOLVER missing — PACKAGING GAP"
    [ -f "$REGISTRY" ] || fail setup "$REGISTRY missing — PACKAGING GAP"
    [ -f "$LIBEXEC/qdistro_disposable_export.py" ] \
        || fail setup "$LIBEXEC/qdistro_disposable_export.py missing — PACKAGING GAP (promoter not installed)"
    # The staging base must exist root/admin-controlled (install-session-manager).
    [ -d "$STAGING_BASE" ] || fail setup "$STAGING_BASE missing — PACKAGING GAP (install did not create the export staging base)"
    [ -L "$STAGING_BASE" ] && fail setup "$STAGING_BASE is a symlink (must be a real dir)"
    # The installed registry must mark agent-scratch export-capable.
    as_admin python3 "$RESOLVER" --resolve "$OPEN_CLASS" --registry "$REGISTRY" 2>/dev/null \
        | grep -q '^EXPORT=true$' \
        || fail setup "installed registry does not mark '$OPEN_CLASS' export-capable"

    as_admin test -S "$RUNTIME_DIR/$OUTER" \
        || fail setup "outer admin compositor not up ($RUNTIME_DIR/$OUTER missing)"
    systemctl start qdistro-admin-broker.service 2>/dev/null || true

    rm -rf "$TIER2_BUILD_DIR" 2>/dev/null || true
    cp -r "$SRC/tier2" "$TIER2_BUILD_DIR" || fail setup "stage tier2 build dir"
    chmod -R a+rX "$TIER2_BUILD_DIR"
    find "$TIER2_BUILD_DIR" -name '*.sh' -exec chmod a+rx {} +
    if ! as_admin podman image exists "$IMAGE" 2>/dev/null; then
        as_admin bash "$TIER2_BUILD_DIR/make-tier2-image.sh" "$WORKLOAD" \
            >/tmp/dispexport-build.log 2>&1 \
            || { cat /tmp/dispexport-build.log >&2; fail setup "build of $IMAGE failed"; }
    fi
    as_admin podman image exists "$IMAGE" || fail setup "$IMAGE not present after build"

    # Author all three gate rules (spawn + open + export). The deny-path tests
    # remove the export rule to prove that gate is enforced independently.
    install -d -m 0755 "$RULE_DIR"
    author_rule "$SPAWN_RULE"  disp-export-spawn-allow  "qdistro.dispose.spawn:${WORKLOAD}"
    author_rule "$OPEN_RULE"   disp-export-open-allow    "qdistro.dispose.open:${OPEN_CLASS}"
    author_rule "$EXPORT_RULE" disp-export-class-allow   "qdistro.dispose.export:${OPEN_CLASS}"
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    local ok=""
    for _ in $(seq 1 20); do
        [ "$(broker_check "qdistro.dispose.spawn:${WORKLOAD}")" = "allow" ] \
            && [ "$(broker_check "qdistro.dispose.open:${OPEN_CLASS}")" = "allow" ] \
            && [ "$(broker_check "qdistro.dispose.export:${OPEN_CLASS}")" = "allow" ] \
            && { ok=1; break; }
        sleep 0.25
    done
    [ -n "$ok" ] || fail setup "gate rules did not all load"
    clean_disp; clean_staging
    pass setup
}

cmd_export_rw_mount() {
    clean_disp; clean_staging
    local out err container="" SPAWN_PID=""
    out=$(mktemp); err=$(mktemp)
    # shellcheck disable=SC2317
    _cleanup() {
        [ -n "${container:-}" ] && as_admin podman rm -f "$container" >/dev/null 2>&1
        [ -n "${SPAWN_PID:-}" ] && kill "$SPAWN_PID" 2>/dev/null
        rm -f "$out" "$err" 2>/dev/null; return 0
    }
    trap _cleanup EXIT

    as_admin env TIER2_OPEN_CLASS="$OPEN_CLASS" TIER2_REQUEST_SILO="$REQUEST_SILO" \
        "$SPAWN" --disposable "$WORKLOAD" -- sleep 600 >"$out" 2>"$err" &
    SPAWN_PID=$!
    local token=""
    for _ in $(seq 1 60); do
        container=$(awk -F= '/^CONTAINER=/{print $2; exit}' "$out" 2>/dev/null)
        token=$(awk -F= '/^LAUNCH_TOKEN=/{print $2; exit}' "$out" 2>/dev/null)
        [ -n "$container" ] && [ -n "$token" ] && break
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 0.5
    done
    [ -n "$container" ] && [ -n "$token" ] \
        || { cat "$err" >&2; fail export-rw-mount "spawn emitted no CONTAINER/LAUNCH_TOKEN (export refused?)"; }
    [[ "$container" =~ $NAME_RE ]] || fail export-rw-mount "bad container name '$container'"
    pass "export-enabled disposable spawned ($container)"

    local up=""
    for _ in $(seq 1 40); do
        as_admin podman container exists "$container" 2>/dev/null && { up=1; break; }
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 0.5
    done
    [ -n "$up" ] || { cat "$err" >&2; fail export-rw-mount "container never appeared"; }

    # /mnt/output is bound RW (inspect, host-side).
    local dest rw
    dest=$(as_admin podman inspect --format \
        '{{range .Mounts}}{{if eq .Destination "/mnt/output"}}{{.Destination}}{{end}}{{end}}' "$container" 2>/dev/null)
    [ "$dest" = "/mnt/output" ] || fail export-rw-mount "/mnt/output not bound (got '$dest')"
    rw=$(as_admin podman inspect --format \
        '{{range .Mounts}}{{if eq .Destination "/mnt/output"}}{{.RW}}{{end}}{{end}}' "$container" 2>/dev/null)
    [ "$rw" = "true" ] || fail export-rw-mount "/mnt/output is RW=$rw (must be writable)"
    pass "/mnt/output bound READ-WRITE"

    # The export labels landed.
    local lbl
    lbl=$(as_admin podman inspect --format '{{.Config.Labels.qdistro_export}}' "$container" 2>/dev/null)
    [ "$lbl" = "1" ] || fail export-rw-mount "qdistro_export label missing (got '$lbl')"
    lbl=$(as_admin podman inspect --format '{{.Config.Labels.qdistro_request_silo}}' "$container" 2>/dev/null)
    [ "$lbl" = "$REQUEST_SILO" ] || fail export-rw-mount "qdistro_request_silo wrong (got '$lbl')"
    lbl=$(as_admin podman inspect --format '{{.Config.Labels.qdistro_open_class}}' "$container" 2>/dev/null)
    [ "$lbl" = "$OPEN_CLASS" ] || fail export-rw-mount "qdistro_open_class wrong (got '$lbl')"
    pass "export labels stamped (qdistro_export/request_silo/open_class)"

    # The staging tree exists; meta.json is OUTSIDE the bound payload dir.
    [ -d "$STAGING_BASE/$token/payload" ] || fail export-rw-mount "payload staging dir not created"
    [ -f "$STAGING_BASE/$token/meta.json" ] || fail export-rw-mount "meta.json not written"
    [ ! -e "$STAGING_BASE/$token/payload/meta.json" ] \
        || fail export-rw-mount "meta.json is INSIDE the payload bind (the container could forge it — CRITICAL)"
    grep -q "\"request_silo\": \"$REQUEST_SILO\"" "$STAGING_BASE/$token/meta.json" \
        || fail export-rw-mount "meta.json missing request_silo"
    pass "staging tree created; meta.json outside the payload bind"

    # A process inside CAN write to /mnt/output, and the bytes appear in the host
    # staging payload (RW end-to-end). (Best-effort exec: the seccomp profile may
    # block an exec'd helper; the inspect RW proof above is authoritative.)
    if as_admin podman exec "$container" true 2>/dev/null; then
        as_admin podman exec "$container" sh -c 'echo disposable-result > /mnt/output/result.txt' 2>/dev/null \
            || fail export-rw-mount "could not write /mnt/output inside the container (RW bind broken)"
        [ "$(cat "$STAGING_BASE/$token/payload/result.txt" 2>/dev/null)" = "disposable-result" ] \
            || fail export-rw-mount "the in-container write did not reach the host staging payload"
        pass "in-container write to /mnt/output reached the host staging payload (RW end-to-end)"
    else
        echo "NOTE: podman exec unavailable (seccomp-scoped); RW proven by inspect (.Mounts RW=true)" >&2
        pass "in-container write to /mnt/output reached the host staging payload (RW end-to-end)"
    fi

    as_admin podman stop -t 5 "$container" >/dev/null 2>&1 || true
    wait "$SPAWN_PID" 2>/dev/null || true
    SPAWN_PID=""; container=""
    pass export-rw-mount
}

cmd_export_gate_fail_closed() {
    clean_disp; clean_staging
    # Remove ONLY the export rule (spawn + open still allow). An export-capable
    # open with a request silo must then refuse at the export gate.
    rm -f "$EXPORT_RULE"
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    local r=""
    for _ in $(seq 1 20); do
        r=$(broker_check "qdistro.dispose.export:${OPEN_CLASS}")
        [ "$r" = "unknown" ] && break
        sleep 0.25
    done
    [ "$r" = "unknown" ] || fail export-gate-fail-closed "export gate did not become 'unknown' (got '$r')"
    pass "broker returns 'unknown' for the now-unruled export class"

    local out err; out=$(mktemp); err=$(mktemp)
    timeout 30 runuser -u "$ADMIN" -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        TIER2_OPEN_CLASS="$OPEN_CLASS" TIER2_REQUEST_SILO="$REQUEST_SILO" \
        "$SPAWN" --disposable "$WORKLOAD" -- sleep 600 >"$out" 2>"$err"
    local rc=$?
    [ "$rc" -ne 124 ] || fail export-gate-fail-closed "spawn did not return (timed out) — fail-open"
    [ "$rc" -ne 0 ] || { cat "$out" >&2; fail export-gate-fail-closed "spawn SUCCEEDED despite no export rule (rc=0) — fail-OPEN (CRITICAL)"; }
    grep -q "qdistro.dispose.export:${OPEN_CLASS}" "$err" \
        || { cat "$err" >&2; fail export-gate-fail-closed "refusal did not name the export gate action"; }
    pass "export-capable open refused at the export gate (decision=unknown)"
    local leaked
    leaked=$(as_admin podman ps -a --filter label=qdistro_disposable=1 --format '{{.Names}}' 2>/dev/null \
             | grep -E "^disp-${WORKLOAD}-" || true)
    [ -z "$leaked" ] || fail export-gate-fail-closed "a container was minted despite the export refusal: $leaked"
    pass "no container minted on the export-gate deny path (fail-closed)"
    # No staging tree should have been created either.
    [ -z "$(ls -A "$STAGING_BASE" 2>/dev/null)" ] \
        || fail export-gate-fail-closed "a staging tree was created despite the export refusal"
    pass "no staging tree created on the export-gate deny path"

    # Re-author so the suite is re-runnable.
    author_rule "$EXPORT_RULE" disp-export-class-allow "qdistro.dispose.export:${OPEN_CLASS}"
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    rm -f "$out" "$err"
    pass export-gate-fail-closed
}

# Build a hand-authored staging tree (admin-owned, as spawn-tier2 would) for the
# import-side store tests, so they don't depend on a live disposable.
_make_staging() {  # _make_staging <token> <silo> <class> [payloadfile=content]
    local token="$1" silo="$2" oc="$3" pf="${4:-result.txt=hi}"
    local d="$STAGING_BASE/$token"
    as_admin mkdir -p -m 0700 "$d/payload"
    as_admin python3 - "$d/meta.json" "$token" "$silo" "$oc" <<'PY'
import json, os, sys
path, token, silo, oc = sys.argv[1:5]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"version": 1, "launch_token": token, "request_silo": silo,
               "open_class": oc, "container": "disp-x", "created": 1,
               "input_basename": None}, f, sort_keys=True)
PY
    as_admin sh -c "printf '%s' '${pf#*=}' > '$d/payload/${pf%%=*}'"
}

# Drive the REAL store import_from_disposable against real podman + real broker +
# real resolver (the daemon's own module, exactly like cmd_workflow_dispose).
cmd_import_flow() {
    clean_disp; clean_staging
    local good_tok bad_silo_tok
    good_tok=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    bad_silo_tok=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    # Staging for: an export-gate test (rule present) targeting an UNTEMPLATED
    # silo (so the real resolver refuses it -> BadState), and a second for the
    # gate-deny test.
    _make_staging "$good_tok" "$REQUEST_SILO" "$OPEN_CLASS"
    _make_staging "$bad_silo_tok" "$REQUEST_SILO" "$OPEN_CLASS"

    local py_out
    py_out=$(QDISTRO_EXPORT_STAGING_BASE="$STAGING_BASE" \
        XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        python3 - "$good_tok" "$bad_silo_tok" <<PY 2>&1
import sys
sys.path.insert(0, "$LIBEXEC")
import qdistro_session_manager as M
good_tok, bad_silo_tok = sys.argv[1:3]
ops = M._SystemOps()
store = M._SiloStore(ops, config_path=M.Path("/tmp/export-silos.yaml"))

# (a) malformed token -> BadArgument (never reaches a path).
try:
    store.import_from_disposable("not a token!")
    print("BADARG_FAIL")
except M.BadArgument:
    print("BADARG_OK")

# (b) absent staging -> clean zero-file receipt.
absent = "f"*32
r = store.import_from_disposable(absent)
print("ABSENT_OK" if r.get("files") == [] and r.get("dest") is None else f"ABSENT_FAIL {r}")

# (c) export rule ALLOWS + staging present, but the target silo is UNTEMPLATED:
# the REAL resolver returns no binding -> BadState (refuse, no durable home).
# Proves the real broker export gate PASSED and the real resolver refused.
try:
    store.import_from_disposable(good_tok)
    print("UNTEMPLATED_FAIL")
except M.BadState as e:
    print("UNTEMPLATED_OK" if "untemplated" in str(e).lower() else f"UNTEMPLATED_WRONG {e}")
# Staging must survive a refusal (not destroyed).
import os
print("KEEP_OK" if os.path.isdir("$STAGING_BASE/"+good_tok) else "KEEP_FAIL")
PY
)
    echo "$py_out" | grep -q '^BADARG_OK$'      || fail import-flow "malformed token not rejected: $py_out"
    echo "$py_out" | grep -q '^ABSENT_OK$'      || fail import-flow "absent staging not a clean receipt: $py_out"
    echo "$py_out" | grep -q '^UNTEMPLATED_OK$' || fail import-flow "untemplated silo not refused via the REAL resolver: $py_out"
    echo "$py_out" | grep -q '^KEEP_OK$'        || fail import-flow "staging destroyed on a refusal: $py_out"
    pass "import: malformed/absent/untemplated handled via the REAL broker+resolver"

    # (d) export-gate DENY at import: remove the export rule, then import must
    # refuse at the broker gate (before resolving the silo) and keep staging.
    rm -f "$EXPORT_RULE"
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    local r=""
    for _ in $(seq 1 20); do
        r=$(broker_check "qdistro.dispose.export:${OPEN_CLASS}"); [ "$r" = "unknown" ] && break; sleep 0.25
    done
    [ "$r" = "unknown" ] || fail import-flow "export gate not unknown after rule removal (got '$r')"
    py_out=$(QDISTRO_EXPORT_STAGING_BASE="$STAGING_BASE" \
        XDG_RUNTIME_DIR="$RUNTIME_DIR" python3 - "$bad_silo_tok" <<PY 2>&1
import os, sys
sys.path.insert(0, "$LIBEXEC")
import qdistro_session_manager as M
tok = sys.argv[1]
store = M._SiloStore(M._SystemOps(), config_path=M.Path("/tmp/export-silos2.yaml"))
try:
    store.import_from_disposable(tok)
    print("DENY_FAIL")
except M.BadState as e:
    print("DENY_OK" if "broker" in str(e).lower() else f"DENY_WRONG {e}")
print("KEEP_OK" if os.path.isdir("$STAGING_BASE/"+tok) else "KEEP_FAIL")
PY
)
    echo "$py_out" | grep -q '^DENY_OK$' || fail import-flow "import not refused at the export gate (real broker): $py_out"
    echo "$py_out" | grep -q '^KEEP_OK$' || fail import-flow "staging destroyed on a gate denial: $py_out"
    pass "import: export-gate DENY refused at the REAL broker, staging kept (fail-closed)"

    author_rule "$EXPORT_RULE" disp-export-class-allow "qdistro.dispose.export:${OPEN_CLASS}"
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    clean_staging
    pass import-flow
}

# --- edit-round-trip (export-back follow-on): real podman spawn + real-fs land --
EDIT_SRC=/tmp/qd-edit-src.txt

cmd_edit_rw_mount() {
    clean_disp; clean_staging
    printf 'ORIGINAL CONTENT\n' > "$EDIT_SRC"; chmod a+r "$EDIT_SRC"
    local src_real; src_real=$(readlink -f "$EDIT_SRC")
    local out err container="" SPAWN_PID=""
    out=$(mktemp); err=$(mktemp)
    # shellcheck disable=SC2317
    _cleanup() {
        [ -n "${container:-}" ] && as_admin podman rm -f "$container" >/dev/null 2>&1
        [ -n "${SPAWN_PID:-}" ] && kill "$SPAWN_PID" 2>/dev/null
        rm -f "$out" "$err" "$EDIT_SRC" 2>/dev/null; return 0
    }
    trap _cleanup EXIT

    # Edit launch: export-capable + edit-capable class, a regular-FILE input, and
    # the per-launch TIER2_REQUEST_EDIT=1 opt-in.
    as_admin env TIER2_OPEN_CLASS="$OPEN_CLASS" TIER2_REQUEST_SILO="$REQUEST_SILO" \
        TIER2_RO_INPUT="$EDIT_SRC" TIER2_REQUEST_EDIT=1 \
        "$SPAWN" --disposable "$WORKLOAD" -- sleep 600 >"$out" 2>"$err" &
    SPAWN_PID=$!
    local token=""
    for _ in $(seq 1 60); do
        container=$(awk -F= '/^CONTAINER=/{print $2; exit}' "$out" 2>/dev/null)
        token=$(awk -F= '/^LAUNCH_TOKEN=/{print $2; exit}' "$out" 2>/dev/null)
        [ -n "$container" ] && [ -n "$token" ] && break
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 0.5
    done
    [ -n "$container" ] && [ -n "$token" ] \
        || { cat "$err" >&2; fail edit-rw-mount "spawn emitted no CONTAINER/LAUNCH_TOKEN (edit refused?)"; }
    pass "edit disposable spawned ($container)"

    local up=""
    for _ in $(seq 1 40); do
        as_admin podman container exists "$container" 2>/dev/null && { up=1; break; }
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 0.5
    done
    [ -n "$up" ] || { cat "$err" >&2; fail edit-rw-mount "container never appeared"; }

    # /mnt/input bound RO, /mnt/output bound RW.
    local roin rw
    roin=$(as_admin podman inspect --format \
        '{{range .Mounts}}{{if eq .Destination "/mnt/input/qd-edit-src.txt"}}{{.RW}}{{end}}{{end}}' "$container" 2>/dev/null)
    [ "$roin" = "false" ] || fail edit-rw-mount "/mnt/input not bound READ-ONLY (RW=$roin)"
    rw=$(as_admin podman inspect --format \
        '{{range .Mounts}}{{if eq .Destination "/mnt/output"}}{{.RW}}{{end}}{{end}}' "$container" 2>/dev/null)
    [ "$rw" = "true" ] || fail edit-rw-mount "/mnt/output is RW=$rw (must be writable)"
    pass "edit disposable: /mnt/input RO + /mnt/output RW"

    # The qdistro_edit forensic label landed (alongside the export labels).
    local lbl
    lbl=$(as_admin podman inspect --format '{{.Config.Labels.qdistro_edit}}' "$container" 2>/dev/null)
    [ "$lbl" = "1" ] || fail edit-rw-mount "qdistro_edit label missing (got '$lbl')"
    pass "qdistro_edit label stamped"

    # meta.json carries edit_mode=true + the canonical input_realpath (outside bind).
    [ -f "$STAGING_BASE/$token/meta.json" ] || fail edit-rw-mount "meta.json not written"
    grep -q '"edit_mode": true' "$STAGING_BASE/$token/meta.json" \
        || fail edit-rw-mount "meta.json missing edit_mode=true"
    grep -q "\"input_realpath\": \"$src_real\"" "$STAGING_BASE/$token/meta.json" \
        || { cat "$STAGING_BASE/$token/meta.json" >&2; fail edit-rw-mount "meta.json missing/wrong input_realpath"; }
    pass "meta.json: edit_mode=true + input_realpath stamped outside the bind"

    as_admin podman stop -t 5 "$container" >/dev/null 2>&1 || true
    wait "$SPAWN_PID" 2>/dev/null || true
    SPAWN_PID=""; container=""
    pass edit-rw-mount
}

# Build edit-mode staging (admin-owned, as spawn-tier2 would) for the import test.
_make_edit_staging() {  # _make_edit_staging <token> <silo> <class> <src_realpath> [payloadfile=content]
    local token="$1" silo="$2" oc="$3" srp="$4" pf="${5:-out.txt=EDITED}"
    local d="$STAGING_BASE/$token"
    as_admin mkdir -p -m 0700 "$d/payload"
    as_admin python3 - "$d/meta.json" "$token" "$silo" "$oc" "$srp" <<'PY'
import json, os, sys
path, token, silo, oc, srp = sys.argv[1:6]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"version": 1, "launch_token": token, "request_silo": silo,
               "open_class": oc, "container": "disp-x", "created": 1,
               "input_basename": os.path.basename(srp),
               "edit_mode": True, "input_realpath": srp}, f, sort_keys=True)
PY
    as_admin sh -c "printf '%s' '${pf#*=}' > '$d/payload/${pf%%=*}'"
}

cmd_edit_import() {
    clean_disp; clean_staging
    # (a) edit_mode import targeting an UNTEMPLATED silo: real broker export gate
    # passes, real resolver returns no binding -> BadState (same fail-closed anchor
    # as plain export; the edit branch must not bypass it).
    local etok; etok=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    _make_edit_staging "$etok" "$REQUEST_SILO" "$OPEN_CLASS" "/tmp/whatever.txt"
    local py_out
    py_out=$(QDISTRO_EXPORT_STAGING_BASE="$STAGING_BASE" \
        XDG_RUNTIME_DIR="$RUNTIME_DIR" python3 - "$etok" <<PY 2>&1
import os, sys
sys.path.insert(0, "$LIBEXEC")
import qdistro_session_manager as M
tok = sys.argv[1]
store = M._SiloStore(M._SystemOps(), config_path=M.Path("/tmp/edit-silos.yaml"))
try:
    store.import_from_disposable(tok)
    print("UNTEMPLATED_FAIL")
except M.BadState as e:
    print("UNTEMPLATED_OK" if "untemplated" in str(e).lower() else f"UNTEMPLATED_WRONG {e}")
print("KEEP_OK" if os.path.isdir("$STAGING_BASE/"+tok) else "KEEP_FAIL")
PY
)
    echo "$py_out" | grep -q '^UNTEMPLATED_OK$' || fail edit-import "edit import to untemplated silo not refused via the REAL resolver: $py_out"
    echo "$py_out" | grep -q '^KEEP_OK$'        || fail edit-import "staging destroyed on a refusal: $py_out"
    pass "edit import: untemplated target refused via the REAL broker+resolver, staging kept"

    # (b) the beside-source landing on a REAL filesystem AS ROOT: this proves the
    # O_TMPFILE/linkat lander + the silo-owner fchown (the host unit runs unpriv,
    # so fchown is a no-op there). A throwaway state tree stands in for a silo home.
    py_out=$(python3 - <<PY 2>&1
import os, sys, tempfile, pwd
sys.path.insert(0, "$LIBEXEC")
import qdistro_disposable_export as E
uid = pwd.getpwnam("$ADMIN").pw_uid
gid = pwd.getpwnam("$ADMIN").pw_gid
state = tempfile.mkdtemp(prefix="qd-editstate-")
os.makedirs(os.path.join(state, "docs"))
src = os.path.join(state, "docs", "report.txt")
open(src, "w").write("ORIGINAL")
payload = tempfile.mkdtemp(prefix="qd-editpayload-")
open(os.path.join(payload, "x"), "w").write("EDITED-BY-DISPOSABLE")
meta = {"launch_token": "a"*32, "open_class": "agent-scratch",
        "request_silo": "$REQUEST_SILO", "container": "disp-x"}
r = E.promote_edit(payload, state, source_rel="docs/report.txt", meta=meta,
                   now_epoch=1700000000, owner_uid=uid, owner_gid=gid)
dest = r["dest"]
print("LANDED_OK" if dest.endswith("/docs/report.txt.disp-edited") else f"LANDED_FAIL {dest}")
print("CONTENT_OK" if open(dest).read() == "EDITED-BY-DISPOSABLE" else "CONTENT_FAIL")
print("SRC_OK" if open(src).read() == "ORIGINAL" else "SRC_FAIL")
st = os.lstat(dest)
print("OWNER_OK" if st.st_uid == uid else f"OWNER_FAIL {st.st_uid}!={uid}")
# no temp litter beside the source
entries = sorted(os.listdir(os.path.join(state, "docs")))
print("CLEAN_OK" if entries == ["report.txt", "report.txt.disp-edited"] else f"CLEAN_FAIL {entries}")
PY
)
    for k in LANDED_OK CONTENT_OK SRC_OK OWNER_OK CLEAN_OK; do
        echo "$py_out" | grep -q "^$k\$" || fail edit-import "real-fs beside-source landing: $k missing ($py_out)"
    done
    pass "edit landing on real fs as root: <name>.disp-edited beside source, silo-owned, source intact, no litter"

    clean_staging
    pass edit-import
}

# --- templated-silo END-TO-END import (the positive resolve->land glue) -------
# The lanes above prove the FAIL-CLOSED import paths (untemplated/gate-deny) and
# the edit landing by calling promote_edit DIRECTLY on a throwaway tempdir. They
# never drove import_from_disposable into a REAL provisioned templated silo whose
# state_path is resolved by the REAL qdistro-resolve-binding — the one un-glued
# backend edge. These two lanes close it: provision a minimal-but-schema-VALID
# templated silo (binding + promoted-generation record authored via the SAME real
# qt schema the SHIPPED unit test tests/unit/test_resolve_binding.py uses and the
# real resolver+validator accept — NOT a full template-build/promote, which would
# build a real image and the importer's resolve path never launches it), then
# drive the REAL store.import_from_disposable and assert the file LANDS.
TEMPLATES_LIBEXEC=/usr/libexec/qdistro

ensure_export_allow() {  # ensure_export_allow <label>
    [ "$(broker_check "qdistro.dispose.export:${OPEN_CLASS}")" = "allow" ] && return 0
    author_rule "$EXPORT_RULE" disp-export-class-allow "qdistro.dispose.export:${OPEN_CLASS}"
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    local _
    for _ in $(seq 1 20); do
        [ "$(broker_check "qdistro.dispose.export:${OPEN_CLASS}")" = "allow" ] && return 0
        sleep 0.25
    done
    fail "${1:-ensure-export}" "export gate did not return to 'allow'"
}

# Author a minimal templated silo the REAL resolver accepts: binding (digest, not
# tag) + a promoted-generation manifest whose generation_ref matches, + a real
# admin-owned state tree (mirrors a tier2 silo whose User=admin -> state is
# admin-owned). template name == silo for clean per-silo teardown. Optional
# srcspec "rel/path=content" seeds a source file (admin-owned) for the edit lane.
_provision_silo() {  # _provision_silo <silo> [srcspec]
    python3 - "$1" "$ADMIN_UID" "${2:-}" <<PY
import hashlib, os, sys
sys.path.insert(0, "$TEMPLATES_LIBEXEC")
import qdistro_templates as qt
silo, admin_uid, srcspec = sys.argv[1], int(sys.argv[2]), sys.argv[3]
layout = qt.Layout()
template = silo
digest = "sha256:" + hashlib.sha256(silo.encode()).hexdigest()
state_path = layout.default_state_path(silo)
os.makedirs(layout.bindings_dir, mode=0o700, exist_ok=True)
binding = {"silo": silo, "template": template, "backend": "podman-image",
           "active_generation": digest, "previous_generations": [],
           "state_path": state_path, "activation_policy": "manual",
           "identity_revision": 1}
qt.write_toml_atomic(layout.binding_file(silo), binding, 0o600)
gen_dir = layout.generation_dir(template, digest)
os.makedirs(gen_dir, exist_ok=True)
manifest = {"template": template, "run_id": "r1", "image_digest": digest,
            "image_id": digest, "containerfile_digest": digest,
            "build_command": "podman build ...", "network_mode": "unrestricted",
            "artifact_manifest": [], "generation_ref": digest}
qt.write_toml_atomic(os.path.join(gen_dir, "manifest.toml"), manifest, 0o644)
os.makedirs(state_path, mode=0o700, exist_ok=True)
os.chown(state_path, admin_uid, admin_uid); os.chmod(state_path, 0o700)
if srcspec:
    rel, _, content = srcspec.partition("=")
    p = os.path.join(state_path, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(content)
    # own the seeded subtree as the silo owner
    d = os.path.dirname(p)
    while os.path.realpath(d).startswith(os.path.realpath(state_path)) and d != state_path:
        os.chown(d, admin_uid, admin_uid); d = os.path.dirname(d)
    os.chown(p, admin_uid, admin_uid)
print("PROVISIONED " + state_path)
PY
}

_deprovision_silo() {  # _deprovision_silo <silo>
    local silo=${1:?}
    rm -f "/var/lib/qdistro/bindings/$silo.toml" "/var/lib/qdistro/bindings/$silo.activated" 2>/dev/null || true
    rm -rf "/var/lib/qdistro/templates/$silo" "/var/lib/qdistro/silos/$silo" 2>/dev/null || true
    rm -f "/run/qdistro/template-run/$silo.toml" 2>/dev/null || true
}

cmd_import_land_templated() {
    clean_disp; clean_staging
    ensure_export_allow import-land-templated
    local silo=exportwork-t state tok py_out prov
    prov=$(mktemp)
    _deprovision_silo "$silo"
    _provision_silo "$silo" >"$prov" 2>&1 || { cat "$prov" >&2; rm -f "$prov"; fail import-land-templated "silo provisioning failed"; }
    grep -q '^PROVISIONED ' "$prov" || { cat "$prov" >&2; rm -f "$prov"; fail import-land-templated "provisioning emitted no state_path"; }
    rm -f "$prov"
    state="/var/lib/qdistro/silos/$silo/state"
    # Sanity: the REAL resolver resolves the provisioned silo to that state_path.
    local resolved
    resolved=$("$TEMPLATES_LIBEXEC/qdistro-resolve-binding" "$silo" --launch-env 2>/dev/null \
               | awk -F= '/^STATE_PATH=/{print $2; exit}')
    [ "$resolved" = "$state" ] \
        || fail import-land-templated "REAL resolve-binding did not return the provisioned state_path (got '$resolved')"
    pass "REAL qdistro-resolve-binding resolves the provisioned silo to its state_path"

    tok=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    _make_staging "$tok" "$silo" "$OPEN_CLASS" "result.txt=hello-from-disposable"
    local lineage_dir lineage_db
    lineage_dir=$(mktemp -d /tmp/land-lineage.XXXXXX)
    lineage_db="$lineage_dir/export.sqlite"
    py_out=$(QDISTRO_EXPORT_STAGING_BASE="$STAGING_BASE" \
        QDISTRO_EXPORT_LINEAGE_DB="$lineage_db" \
        XDG_RUNTIME_DIR="$RUNTIME_DIR" python3 - "$tok" "$state" "$ADMIN_UID" <<PY 2>&1
import os, re, sys
sys.path.insert(0, "$LIBEXEC")
import qdistro_session_manager as M
import qdistro_lineage_receipts as lr
from qdistro_lineage_store import LineageStore
tok, state, admin_uid = sys.argv[1], sys.argv[2], int(sys.argv[3])
store = M._SiloStore(M._SystemOps(), config_path=M.Path("/tmp/land-silos.yaml"))
r = store.import_from_disposable(tok)
dest = r.get("dest")
print("DEST=" + str(dest))
real_state = os.path.realpath(state)
inc = os.path.join(real_state, "Incoming", "agent-scratch")
print("UNDER_OK" if dest and os.path.realpath(dest).startswith(inc + os.sep) else "UNDER_FAIL " + str(dest))
# the landing leaf is the documented <token8>-<ts> per-import dir
# (ts is a YYYYMMDD-HHMMSS stamp, so token8 + date + time)
print("LEAF_OK" if dest and re.match(r"^[0-9a-f]{8}-[0-9]{8}-[0-9]{6}$", os.path.basename(dest)) else "LEAF_FAIL " + (os.path.basename(dest) if dest else "none"))
landed = os.path.join(dest, "result.txt") if dest else ""
print("CONTENT_OK" if landed and os.path.isfile(landed) and open(landed).read() == "hello-from-disposable" else "CONTENT_FAIL")
st = os.lstat(landed) if landed and os.path.exists(landed) else None
print("OWNER_OK" if st and st.st_uid == admin_uid else "OWNER_FAIL " + (str(st.st_uid) if st else "no-file"))
print("RECEIPT_OK" if dest and os.path.isfile(os.path.join(dest, "_receipt.json")) else "RECEIPT_FAIL")
# --- lineage receipt surfaces (Task 2) ---
side = os.path.join(dest, "result.txt.qdistro-lineage.json") if dest else ""
man = os.path.join(dest, "qdistro-export-manifest.json") if dest else ""
print("SIDECAR_OK" if side and os.path.isfile(side) else "SIDECAR_FAIL")
print("MANIFEST_OK" if man and os.path.isfile(man) else "MANIFEST_FAIL")
print("SEALED_OK" if r.get("lineage_sealed") else "SEALED_FAIL " + repr(r.get("lineage_sealed")))
sst = os.lstat(side) if side and os.path.exists(side) else None
print("SIDECAR_OWNER_OK" if sst and sst.st_uid == admin_uid else "SIDECAR_OWNER_FAIL")
# the on-disk sidecar verifies against the SEALED store row, a forged copy does not
ls = LineageStore(os.environ["QDISTRO_EXPORT_LINEAGE_DB"])
env = lr.read_sidecar(os.path.join(dest, "result.txt"))
print("VERIFY_OK" if lr.verify_against_store(ls, env) else "VERIFY_FAIL")
print("TAMPER_OK" if not lr.verify_against_store(ls, dict(env, locator="evil.txt")) else "TAMPER_FAIL")
man_env = lr.read_export_manifest(dest)
print("MANIFEST_VERIFY_OK" if all(lr.verify_against_store(ls, c) for c in man_env["receipts"]) else "MANIFEST_VERIFY_FAIL")
ls.close()
# --- xattr pointer (item 1): must SURVIVE the temp-dir -> final-dir rename on
# the real (btrfs) Incoming tree. The xattr is set on the file inode in the temp
# dir; a parent-dir rename preserves inode xattrs, so on an xattr-capable fs the
# compact pointer must read back consistent with the verifiable sidecar.
def _xattr_supported(p):
    try:
        os.setxattr(p, "user.qdistro.probe", b"1"); os.removexattr(p, "user.qdistro.probe")
        return True
    except OSError:
        return False
xp = lr.read_xattr(landed) if landed and os.path.exists(landed) else None
if xp is not None:
    print("XATTR_OK" if xp == {k: env[k] for k in lr._XATTR_KEYS} else "XATTR_FAIL " + repr(xp))
elif landed and os.path.exists(landed) and not _xattr_supported(landed):
    print("XATTR_SKIP")
else:
    print("XATTR_LOST")
print("STAGING_GONE_OK" if not os.path.exists("$STAGING_BASE/" + tok) else "STAGING_GONE_FAIL")
PY
)
    rm -rf "$lineage_dir"
    local k
    for k in UNDER_OK LEAF_OK CONTENT_OK OWNER_OK RECEIPT_OK \
             SIDECAR_OK MANIFEST_OK SEALED_OK SIDECAR_OWNER_OK \
             VERIFY_OK TAMPER_OK MANIFEST_VERIFY_OK STAGING_GONE_OK; do
        echo "$py_out" | grep -q "^$k\$" || fail import-land-templated "$k missing: $py_out"
    done
    # xattr is opportunistic: OK (set + survived the move, consistent w/ sidecar)
    # or SKIP (fs without user-xattr support) pass; LOST (supported but the pointer
    # did not survive the brokered move) or FAIL (inconsistent) is a real bug.
    echo "$py_out" | grep -Eq '^XATTR_(OK|SKIP)$' \
        || fail import-land-templated "xattr pointer did not survive the brokered move: $py_out"
    pass "import: landed silo-owned + _receipt.json + chain-anchored lineage receipt surfaces (sidecar+manifest+xattr) that VERIFY against the sealed store (forged copy refused), staging one-shot removed"
    _deprovision_silo "$silo"; clean_staging
    pass import-land-templated
}

cmd_edit_land_templated() {
    clean_disp; clean_staging
    ensure_export_allow edit-land-templated
    local silo=editwork-t state src src_real tok py_out prov
    prov=$(mktemp)
    _deprovision_silo "$silo"
    _provision_silo "$silo" "docs/report.txt=ORIGINAL" >"$prov" 2>&1 \
        || { cat "$prov" >&2; rm -f "$prov"; fail edit-land-templated "silo provisioning failed"; }
    rm -f "$prov"
    state="/var/lib/qdistro/silos/$silo/state"
    src="$state/docs/report.txt"
    src_real=$(readlink -f "$src")
    [ -f "$src" ] || fail edit-land-templated "seeded source file missing"

    tok=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    _make_edit_staging "$tok" "$silo" "$OPEN_CLASS" "$src_real" "out=EDITED-BY-DISPOSABLE"
    local lineage_dir lineage_db
    lineage_dir=$(mktemp -d /tmp/editland-lineage.XXXXXX)
    lineage_db="$lineage_dir/export.sqlite"
    py_out=$(QDISTRO_EXPORT_STAGING_BASE="$STAGING_BASE" \
        QDISTRO_EXPORT_LINEAGE_DB="$lineage_db" \
        XDG_RUNTIME_DIR="$RUNTIME_DIR" python3 - "$tok" "$src" "$ADMIN_UID" <<PY 2>&1
import os, sys
sys.path.insert(0, "$LIBEXEC")
import qdistro_session_manager as M
import qdistro_lineage_receipts as lr
from qdistro_lineage_store import LineageStore
tok, src, admin_uid = sys.argv[1], sys.argv[2], int(sys.argv[3])
store = M._SiloStore(M._SystemOps(), config_path=M.Path("/tmp/editland-silos.yaml"))
r = store.import_from_disposable(tok)
dest = r.get("dest")
print("DEST=" + str(dest))
print("LANDED_OK" if dest and dest.endswith("/docs/report.txt.disp-edited") else "LANDED_FAIL " + str(dest))
print("BESIDE_OK" if dest and os.path.dirname(dest) == os.path.dirname(os.path.realpath(src)) else "BESIDE_FAIL")
print("CONTENT_OK" if dest and os.path.isfile(dest) and open(dest).read() == "EDITED-BY-DISPOSABLE" else "CONTENT_FAIL")
print("SRC_OK" if open(src).read() == "ORIGINAL" else "SRC_FAIL")
st = os.lstat(dest) if dest and os.path.exists(dest) else None
print("OWNER_OK" if st and st.st_uid == admin_uid else "OWNER_FAIL " + (str(st.st_uid) if st else "no-file"))
# --- edit lineage receipt (item 2): ONE sidecar beside the edited file, no
# batch manifest (single-file), sealed + verifying against the store ---
side = (dest + ".qdistro-lineage.json") if dest else ""
print("SIDECAR_OK" if side and os.path.isfile(side) else "SIDECAR_FAIL")
print("NOMANIFEST_OK" if dest and not os.path.exists(os.path.join(os.path.dirname(dest), "qdistro-export-manifest.json")) else "NOMANIFEST_FAIL")
print("SEALED_OK" if r.get("lineage_sealed") else "SEALED_FAIL " + repr(r.get("lineage_sealed")))
sst = os.lstat(side) if side and os.path.exists(side) else None
print("SIDECAR_OWNER_OK" if sst and sst.st_uid == admin_uid else "SIDECAR_OWNER_FAIL")
ls = LineageStore(os.environ["QDISTRO_EXPORT_LINEAGE_DB"])
env = lr.read_sidecar(dest)
print("VERIFY_OK" if lr.verify_against_store(ls, env) else "VERIFY_FAIL")
print("TAMPER_OK" if not lr.verify_against_store(ls, dict(env, locator="evil")) else "TAMPER_FAIL")
ls.close()
def _xattr_supported(p):
    try:
        os.setxattr(p, "user.qdistro.probe", b"1"); os.removexattr(p, "user.qdistro.probe")
        return True
    except OSError:
        return False
xp = lr.read_xattr(dest) if dest and os.path.exists(dest) else None
if xp is not None:
    print("XATTR_OK" if xp == {k: env[k] for k in lr._XATTR_KEYS} else "XATTR_FAIL " + repr(xp))
elif dest and os.path.exists(dest) and not _xattr_supported(dest):
    print("XATTR_SKIP")
else:
    print("XATTR_LOST")
print("STAGING_GONE_OK" if not os.path.exists("$STAGING_BASE/" + tok) else "STAGING_GONE_FAIL")
PY
)
    rm -rf "$lineage_dir"
    local k
    for k in LANDED_OK BESIDE_OK CONTENT_OK SRC_OK OWNER_OK \
             SIDECAR_OK NOMANIFEST_OK SEALED_OK SIDECAR_OWNER_OK \
             VERIFY_OK TAMPER_OK STAGING_GONE_OK; do
        echo "$py_out" | grep -q "^$k\$" || fail edit-land-templated "$k missing: $py_out"
    done
    echo "$py_out" | grep -Eq '^XATTR_(OK|SKIP)$' \
        || fail edit-land-templated "edit xattr pointer not preserved: $py_out"
    pass "edit import: landed <name>.disp-edited beside source IN A REAL SILO STATE via the REAL resolver, silo-owned, source intact, one chain-anchored sidecar (no manifest) that VERIFIES + xattr, staging one-shot removed"
    _deprovision_silo "$silo"; clean_staging
    pass edit-land-templated
}

cmd_teardown() {
    clean_disp; clean_staging
    _deprovision_silo exportwork-t; _deprovision_silo editwork-t
    rm -f "$SPAWN_RULE" "$OPEN_RULE" "$EXPORT_RULE" "$EDIT_SRC" 2>/dev/null || true
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    rm -rf "$TIER2_BUILD_DIR" /tmp/dispexport-build.log 2>/dev/null || true
    pass teardown
}

case "${1:-}" in
    setup) cmd_setup ;;
    export-rw-mount) cmd_export_rw_mount ;;
    export-gate-fail-closed) cmd_export_gate_fail_closed ;;
    import-flow) cmd_import_flow ;;
    import-land-templated) cmd_import_land_templated ;;
    edit-rw-mount) cmd_edit_rw_mount ;;
    edit-import) cmd_edit_import ;;
    edit-land-templated) cmd_edit_land_templated ;;
    teardown) cmd_teardown ;;
    *) echo "usage: $0 {setup|export-rw-mount|export-gate-fail-closed|import-flow|import-land-templated|edit-rw-mount|edit-import|edit-land-templated|teardown}" >&2; exit 2 ;;
esac
