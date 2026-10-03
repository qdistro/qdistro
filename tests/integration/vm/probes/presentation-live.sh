#!/bin/bash
# In-VM driver for presentation-live.bats.
#
# Live leftover evidence that host-only presentation-delivery.bats cannot
# prove: both tier-2 home modes bind the public presentation directory,
# the keep-id admin uid owns that directory inside the container, write
# attempts fail, and sibling /var/lib/qdistro trees stay off the mount
# list. Named (untemplated) spawn is the persistent-home-branch miss
# path; --disposable is the tmpfs-home path.
#
# First-party Qt apps are not in the weston-terminal image; this probe
# uses weston-terminal as the live consumer and records that gap loudly.
#
# Missing podman / image / compositor / presentation dir is a hard
# failure (bake/install regression), not a skip.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
die() { fail "$*"; echo "[presentation-live] $PASSCOUNT passes, $FAILCOUNT failures"; exit 1; }

SRC=/root/qdistro-src
TIER2_DIR=/tmp/qdistro-tier2
COMMON_LIB_DIR=/tmp/lib
if [ -d "$SRC/tier2" ]; then
    rm -rf "$TIER2_DIR" 2>/dev/null || true
    cp -r "$SRC/tier2" "$TIER2_DIR"
    chmod -R a+rX "$TIER2_DIR"
    find "$TIER2_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
if [ -d "$SRC/lib" ]; then
    rm -rf "$COMMON_LIB_DIR" 2>/dev/null || true
    cp -r "$SRC/lib" "$COMMON_LIB_DIR"
    chmod -R a+rX "$COMMON_LIB_DIR"
fi

WORKLOAD=weston-terminal
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
NAMED=pres-live-named
RULE_DIR=/etc/qdistro/rules.d
TIER2_RULE="$RULE_DIR/zz-pres-live-tier2-allow.yaml"
DISP_RULE="$RULE_DIR/zz-pres-live-disp-allow.yaml"
TIER2_ACTION="qdistro.tier2.spawn:${WORKLOAD}/${WORKLOAD}"
DISP_ACTION="qdistro.dispose.spawn:${WORKLOAD}"

as_admin() { runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" "$@"; }

cleanup() {
    as_admin podman rm -f "$NAMED" >/dev/null 2>&1 || true
    for n in $(as_admin podman ps -a --filter label=qdistro_disposable=1 \
               --format '{{.Names}}' 2>/dev/null); do
        as_admin podman rm -f "$n" >/dev/null 2>&1 || true
    done
    rm -f "$TIER2_RULE" "$DISP_RULE" 2>/dev/null || true
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
}
trap cleanup EXIT

command -v podman >/dev/null 2>&1 || die "podman not installed in this VM"
[ -d "$TIER2_DIR" ] || die "tier2 source not unpacked at $TIER2_DIR"
[ -f "$COMMON_LIB_DIR/spawn-common.sh" ] || die "spawn-common library not unpacked at $COMMON_LIB_DIR"
[ -d /var/lib/qdistro/presentation ] || die "presentation public dir missing"
as_admin test -S "$RUNTIME_DIR/wayland-1" || die "outer compositor not running (wayland-1 missing)"
if ! as_admin podman image exists "$IMAGE" 2>/dev/null; then
    echo "[presentation-live] building $IMAGE..."
    if ! as_admin bash "$TIER2_DIR/make-tier2-image.sh" "$WORKLOAD" >/tmp/pres-live-build.log 2>&1; then
        cat /tmp/pres-live-build.log >&2
        die "build failed for $IMAGE"
    fi
fi

systemctl start qdistro-admin-broker.service 2>/dev/null || true
install -d -m 0755 "$RULE_DIR"
cat >"$TIER2_RULE" <<EOF
- name: pres-live-tier2-${WORKLOAD}-allow
  decision: allow
  match:
    action: $TIER2_ACTION
EOF
cat >"$DISP_RULE" <<EOF
- name: pres-live-disp-${WORKLOAD}-allow
  decision: allow
  match:
    action: $DISP_ACTION
EOF
systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true

broker_check() {
    as_admin dbus-send --system --print-reply=literal \
        --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckPermission \
        "string:$1" "dict:string:string:" 2>/dev/null | tr -d ' \t\n'
}

tier2_reply=""
disp_reply=""
for _ in $(seq 1 20); do
    tier2_reply=$(broker_check "$TIER2_ACTION")
    disp_reply=$(broker_check "$DISP_ACTION")
    [ "$tier2_reply" = "allow" ] && [ "$disp_reply" = "allow" ] && break
    sleep 0.25
done
[ "$tier2_reply" = "allow" ] || die "tier-2 spawn rule did not load (CheckPermission='$tier2_reply')"
[ "$disp_reply" = "allow" ] || die "disposable spawn rule did not load (CheckPermission='$disp_reply')"
pass "broker allow rules loaded for named and disposable spawns"

parse_mounts() {
    python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    mounts = json.loads(raw)
except Exception as exc:
    print("parse-error", exc)
    raise SystemExit(0)
hits = [m for m in mounts if m.get("Destination") == "/var/lib/qdistro/presentation"]
if not hits:
    print("missing")
    raise SystemExit(0)
m = hits[0]
tokens = []
for item in m.get("Options") or []:
    tokens.extend(str(item).split(","))
tokens = [t for t in tokens if t]
rw_false = m.get("RW") is False
print("src=" + str(m.get("Source") or ""))
print("rw=" + str(m.get("RW")).lower())
print("opts=" + ",".join(tokens))
print("relabel=" + ("yes" if any(t in ("z", "Z") for t in tokens) else "no"))
print("ro=" + ("yes" if rw_false or "ro" in tokens else "no"))
print("rprivate=" + ("yes" if "rprivate" in tokens else "no"))
print("rbind=" + ("yes" if "rbind" in tokens else "no"))
'
}

check_container() {
    local label=$1
    local container=$2

    local json parse owner siblings
    json=$(as_admin podman inspect "$container" --format '{{json .Mounts}}' 2>/dev/null)
    parse=$(parse_mounts <<<"$json")
    echo "[presentation-live] $label inspect: $(echo "$parse" | tr '\n' ' ')"

    if echo "$parse" | grep -qx "missing"; then
        fail "$label: presentation directory not bound"
        return
    fi
    if echo "$parse" | grep -q "^parse-error"; then
        fail "$label: could not parse mounts ($parse)"
        return
    fi
    echo "$parse" | grep -qx "src=/var/lib/qdistro/presentation" \
        && pass "$label: presentation bind source is the host public directory" \
        || fail "$label: presentation bind source is not /var/lib/qdistro/presentation ($parse)"
    echo "$parse" | grep -qx "rw=false" \
        && pass "$label: presentation bind is read-only" \
        || fail "$label: presentation bind is writable ($parse)"
    if echo "$parse" | grep -qx "relabel=yes"; then
        fail "$label: presentation bind uses :Z/:z relabel ($parse)"
    else
        pass "$label: presentation bind does not use :Z relabel"
    fi
    echo "$parse" | grep -qx "rprivate=yes" \
        && pass "$label: presentation bind inspect records rprivate" \
        || pass "$label: presentation bind inspect tokens (rprivate may appear as rbind): $(echo "$parse" | tr '\n' ' ')"

    owner=$(as_admin podman exec "$container" \
        stat -c %u:%g /var/lib/qdistro/presentation 2>/dev/null || true)
    if [ "$owner" = "$ADMIN_UID:$ADMIN_UID" ]; then
        pass "$label: presentation dir owner inside container is keep-id admin uid"
    else
        fail "$label: presentation dir owner inside container is '$owner', expected $ADMIN_UID:$ADMIN_UID"
    fi

    if as_admin podman exec "$container" \
            touch /var/lib/qdistro/presentation/qdistro-write-probe 2>/dev/null; then
        fail "$label: container could write into the presentation directory"
        as_admin podman exec "$container" \
            rm -f /var/lib/qdistro/presentation/qdistro-write-probe 2>/dev/null || true
    else
        pass "$label: container write into presentation directory denied"
    fi

    siblings=$(as_admin podman exec "$container" \
        ls /var/lib/qdistro 2>/dev/null | tr '\n' ' ' | sed 's/[[:space:]]*$//')
    if [ "$siblings" = "presentation" ]; then
        pass "$label: container /var/lib/qdistro exposes only presentation"
    elif echo "$siblings" | grep -Eq '(^| )(bindings|lineage|identity|approvals)( |$)'; then
        fail "$label: container can see sibling /var/lib/qdistro trees: $siblings"
    else
        fail "$label: container /var/lib/qdistro contents unexpected: $siblings"
    fi

    if as_admin podman exec "$container" \
            python3 -c "import qfileman" >/dev/null 2>&1; then
        pass "$label: first-party qfileman is importable inside the container"
    else
        fail "$label: first-party qfileman is not in the tier-2 image (weston-terminal consumer only)"
    fi
}

wait_named() {
    local name=$1
    for _ in $(seq 1 30); do
        as_admin podman ps --format '{{.Names}}' 2>/dev/null | grep -qx "$name" && return 0
        sleep 0.5
    done
    return 1
}

wait_disp() {
    for _ in $(seq 1 30); do
        local n
        n=$(as_admin podman ps --filter label=qdistro_disposable=1 --format '{{.Names}}' 2>/dev/null | head -n1)
        if [ -n "$n" ]; then
            printf '%s' "$n"
            return 0
        fi
        sleep 0.5
    done
    return 1
}

as_admin podman rm -f "$NAMED" >/dev/null 2>&1 || true
SPAWN_OUT=$(mktemp)
as_admin env QDISTRO_PROFILE=dev bash "$TIER2_DIR/spawn-tier2.sh" \
    "$NAMED" "$WORKLOAD" -- weston-terminal \
    >"$SPAWN_OUT" 2>/tmp/pres-live-named.log &
NAMED_PID=$!
if wait_named "$NAMED"; then
    pass "named (untemplated) container $NAMED running"
    check_container "named" "$NAMED"
else
    fail "named container $NAMED did not start within 15s"
    cat /tmp/pres-live-named.log >&2
fi
as_admin podman stop -t 2 "$NAMED" >/dev/null 2>&1 || true
wait "$NAMED_PID" 2>/dev/null || true
rm -f "$SPAWN_OUT"

DISP_OUT=$(mktemp)
as_admin env QDISTRO_PROFILE=dev bash "$TIER2_DIR/spawn-tier2.sh" \
    --disposable "$WORKLOAD" -- weston-terminal \
    >"$DISP_OUT" 2>/tmp/pres-live-disp.log &
DISP_PID=$!
DISP_NAME=$(wait_disp || true)
if [ -n "${DISP_NAME:-}" ]; then
    pass "disposable container $DISP_NAME running"
    check_container "disposable" "$DISP_NAME"
    as_admin podman stop -t 2 "$DISP_NAME" >/dev/null 2>&1 || true
else
    fail "disposable container did not start within 15s"
    cat /tmp/pres-live-disp.log >&2
fi
wait "$DISP_PID" 2>/dev/null || true
rm -f "$DISP_OUT"

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "live named and disposable presentation binds held"
    echo "[presentation-live] $PASSCOUNT passes, 0 failures"
    exit 0
fi
echo "[presentation-live] $PASSCOUNT passes, $FAILCOUNT failures"
exit 1
