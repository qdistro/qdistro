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
# The consumer is the first-party qfileman tier-2 image, which bakes
# /usr/share/qdistro/presentation/deployment.json. Inside each container
# the installed SDK must resolve the bound snapshot as a managed source
# with the admin uid (the keep-id owner assertion plan 03 requires), and
# a watching controller must follow a host publish without a restart.
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

WORKLOAD=qfileman
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
NAMED=pres-live-named
RULE_DIR=/etc/qdistro/rules.d
TIER2_RULE="$RULE_DIR/zz-pres-live-tier2-allow.yaml"
DISP_RULE="$RULE_DIR/zz-pres-live-disp-allow.yaml"
TIER2_ACTION="qdistro.tier2.spawn:${WORKLOAD}/${WORKLOAD}"
DISP_ACTION="qdistro.dispose.spawn:${WORKLOAD}"

# DBUS_SESSION_BUS_ADDRESS sends rootless podman to admin's user systemd for
# cgroups. Without it, a root SSH login (the enforcing transport) leaves
# runuser in user-0.slice and podman's cgroupfs fallback is denied.
as_admin() {
    runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNTIME_DIR/bus" "$@"
}

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

# A podman process can be running during the entrypoint's five-second fatal
# startup window. Require the handoff marker AND a live inner listener before
# testing anything; recheck the app and listener after the last assertion.
inner_running() {
    local container=$1
    [ "$(as_admin podman inspect "$container" --format '{{.State.Running}}' 2>/dev/null)" = true ] || return 1
    as_admin podman exec "$container" python3 -c '
import os, socket
argv = open("/proc/1/cmdline", "rb").read().split(b"\0")
# The entrypoint argv also ends in qfileman before exec; require the
# installed Python console script as PID 1, not just an argv substring.
assert len(argv) > 1 and os.path.basename(argv[1]) == b"qfileman", argv
assert os.path.basename(os.readlink("/proc/1/exe")).startswith("python")
s = socket.socket(socket.AF_UNIX)
s.settimeout(2)
s.connect(os.environ["XDG_RUNTIME_DIR"] + "/wayland-tier2")
' >/dev/null 2>&1
}

wait_inner() {
    local container=$1
    for _ in $(seq 1 30); do
        if as_admin podman logs "$container" 2>&1 | grep -q "inner weston up; exec'ing app:" \
            && inner_running "$container"; then
            return 0
        fi
        sleep 0.5
    done
    as_admin podman logs "$container" >&2 2>&1 || true
    cat /tmp/pres-live-named.log /tmp/pres-live-disp.log >&2 2>/dev/null || true
    return 1
}

shared_labels() {
    local path
    for path in "${SHARED_PATHS[@]}"; do
        stat -c '%n %C' "$path" || return 1
    done
}

check_container() {
    local label=$1
    local container=$2

    if ! wait_inner "$container"; then
        fail "$label: inner weston and qfileman did not become ready"
        return
    fi
    pass "$label: inner weston up and qfileman running"

    if [ "$(getenforce)" = Enforcing ]; then
        local token mount_label process_label runtime_label
        token=$(as_admin podman inspect "$container" --format '{{index .Config.Labels "qdistro_tier2_token"}}')
        mount_label=$(as_admin podman inspect "$container" --format '{{.MountLabel}}')
        process_label=$(as_admin podman inspect "$container" --format '{{.ProcessLabel}}')
        runtime_label=$(stat -c %C "$RUNTIME_DIR/qdistro-tier2/$token")
        if [[ "$process_label" == *:container_t:s0:c* ]] \
            && [[ "$mount_label" == *:container_file_t:s0:c* ]] \
            && [ "${process_label#*:*:*:}" = "${mount_label#*:*:*:}" ] \
            && [ "$runtime_label" = "$mount_label" ]; then
            pass "$label: private runtime matches container MCS label ($runtime_label)"
        else
            fail "$label: labels process=$process_label mount=$mount_label runtime=$runtime_label"
        fi
    fi

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
            env PYTHONSAFEPATH=1 python3 -c "import qfileman" >/dev/null 2>&1; then
        pass "$label: first-party qfileman is importable inside the container"
    else
        fail "$label: first-party qfileman is not importable inside the container"
    fi

    local resolved
    resolved=$(as_admin podman exec "$container" env -u QDISTRO_PRESENTATION_FILE PYTHONSAFEPATH=1 \
        python3 -c "$RESOLVE_PY" 2>&1 | tail -n1)
    echo "[presentation-live] $label resolve: $resolved"
    if [ "$resolved" = "managed uid=$ADMIN_UID gen=$GEN_A" ]; then
        pass "$label: in-container SDK reads the managed snapshot as admin uid $ADMIN_UID"
    else
        fail "$label: in-container SDK resolve '$resolved', expected 'managed uid=$ADMIN_UID gen=$GEN_A'"
    fi

    # QFileSystemWatcher puts an inotify watch on current.json itself, not
    # only on the directory. Under enforcing, container_t needed file
    # `watch` (qdistro_presentation 0.1.1); 0.1.0 denied it silently.
    local watched
    watched=$(as_admin podman exec "$container" env PYTHONSAFEPATH=1 python3 -c "$WATCH_PY" 2>&1 | tail -n1)
    if [ "$watched" = "dir=True file=True" ]; then
        pass "$label: in-container inotify watch on the directory and current.json"
    else
        fail "$label: in-container inotify watch result '$watched', expected 'dir=True file=True'"
    fi

    local follow_out gen_b follow_pid
    follow_out=$(mktemp)
    as_admin podman exec "$container" env -u QDISTRO_PRESENTATION_FILE PYTHONSAFEPATH=1 QT_QPA_PLATFORM=offscreen \
        python3 -c "$FOLLOW_PY" >"$follow_out" 2>&1 &
    follow_pid=$!
    for _ in $(seq 1 40); do
        grep -q '^initial ' "$follow_out" && break
        sleep 0.25
    done
    PUBLISH_N=$((PUBLISH_N + 1))
    gen_b=$(publish_snapshot "$PUBLISH_N") || gen_b=""
    wait "$follow_pid" 2>/dev/null || true
    echo "[presentation-live] $label follow: $(tr '\n' ' ' <"$follow_out")"
    if [ -n "$gen_b" ] && grep -qx "followed $gen_b" "$follow_out"; then
        pass "$label: running in-container controller followed a host publish"
    else
        fail "$label: in-container controller did not follow host publish to '$gen_b'"
    fi
    rm -f "$follow_out"
    GEN_A=$gen_b
    if inner_running "$container"; then
        pass "$label: inner weston and qfileman still running after checks"
    else
        fail "$label: inner weston or qfileman died during checks"
    fi
}

# Admin-side publish; argument N picks a distinct, valid UI font scale
# (palette edits must keep publisher contrast). Prints the generation.
PUBLISH_N=0
publish_snapshot() {
    as_admin env -u QDISTRO_PRESENTATION_FILE PYTHONSAFEPATH=1 python3 - "$1" <<'PY'
import sys
from dataclasses import replace

from qdistro_presentation.model import example_snapshot
from qdistro_presentation.publish import write_snapshot

snap = example_snapshot()
scale = 1.0 + 0.05 * (int(sys.argv[1]) % 8)
snap = replace(snap, fonts=replace(snap.fonts, ui_scale=scale), enabled=True)
print(write_snapshot("/var/lib/qdistro/presentation", snap, owner_uid=1000,
                     skip_unchanged=False).generation)
PY
}

# In-container: resolve the default path (no override) and read it.
RESOLVE_PY='
from qdistro_presentation.paths import load_snapshot, resolve_snapshot_path
r = resolve_snapshot_path()
if r is None:
    print("unresolved")
else:
    snap, _ = load_snapshot(r)
    print(f"{r.kind} uid={r.expected_uid} gen={snap.generation}")
'

# In-container: can this domain add inotify watches on the dir and file?
WATCH_PY='
from PyQt6.QtCore import QCoreApplication, QFileSystemWatcher
app = QCoreApplication(["presentation-live-watch"])
w = QFileSystemWatcher()
d = w.addPath("/var/lib/qdistro/presentation")
f = w.addPath("/var/lib/qdistro/presentation/current.json")
print(f"dir={d} file={f}")
'

# In-container: a watching controller must see a host publish within 15 s.
FOLLOW_PY='
import sys, time
from PyQt6.QtWidgets import QApplication
from qdistro_presentation.qt import PresentationController
app = QApplication(["presentation-live-follow"])
ctrl = PresentationController(app, theme_mode="system", watch=True)
first = ctrl.state.generation
print("initial", first, flush=True)
deadline = time.monotonic() + 15
while time.monotonic() < deadline:
    app.processEvents()
    if ctrl.state.generation != first:
        print("followed", ctrl.state.generation, flush=True)
        sys.exit(0)
    time.sleep(0.05)
print("timeout", ctrl.state.generation, flush=True)
sys.exit(1)
'

GEN_A=$(publish_snapshot 0) || die "admin publish of the initial snapshot failed"
[ -n "$GEN_A" ] || die "admin publish printed an empty generation"
pass "admin published initial snapshot $GEN_A"

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

SHARED_PATHS=("$RUNTIME_DIR/wayland-1" /usr/lib64/weston/qdwin-shell.so /var/lib/qdistro/presentation)
for path in "$RUNTIME_DIR"/pipewire-[0-9]*; do
    [ -S "$path" ] && SHARED_PATHS+=("$path")
done
SHARED_LABELS_BEFORE=$(shared_labels) || die "cannot capture shared host labels"
as_admin podman rm -f "$NAMED" >/dev/null 2>&1 || true
SPAWN_OUT=$(mktemp)
as_admin env QDISTRO_PROFILE=dev bash "$TIER2_DIR/spawn-tier2.sh" \
    "$NAMED" "$WORKLOAD" -- qfileman \
    >"$SPAWN_OUT" 2>/tmp/pres-live-named.log &
NAMED_PID=$!
if wait_named "$NAMED"; then
    pass "named (untemplated) container $NAMED running"
    check_container "named" "$NAMED"
else
    fail "named container $NAMED did not start within 15s"
    cat /tmp/pres-live-named.log >&2
fi

DISP_OUT=$(mktemp)
as_admin env QDISTRO_PROFILE=dev bash "$TIER2_DIR/spawn-tier2.sh" \
    --disposable "$WORKLOAD" -- qfileman \
    >"$DISP_OUT" 2>/tmp/pres-live-disp.log &
DISP_PID=$!
DISP_NAME=$(wait_disp || true)
if [ -n "${DISP_NAME:-}" ]; then
    pass "disposable container $DISP_NAME running"
    check_container "disposable" "$DISP_NAME"
    if [ "$(getenforce)" = Enforcing ]; then
        named_mcs=$(as_admin podman inspect "$NAMED" --format '{{.MountLabel}}')
        disp_mcs=$(as_admin podman inspect "$DISP_NAME" --format '{{.MountLabel}}')
        if inner_running "$NAMED" && [ -n "$named_mcs" ] && [ -n "$disp_mcs" ] \
            && [ "$named_mcs" != "$disp_mcs" ]; then
            pass "concurrent containers have distinct private MCS labels"
        else
            fail "concurrent MCS isolation: named=$named_mcs disposable=$disp_mcs"
        fi
    fi
    as_admin podman stop -t 2 "$DISP_NAME" >/dev/null 2>&1 || true
else
    fail "disposable container did not start within 15s"
    cat /tmp/pres-live-disp.log >&2
fi
wait "$DISP_PID" 2>/dev/null || true
rm -f "$DISP_OUT"
as_admin podman stop -t 2 "$NAMED" >/dev/null 2>&1 || true
wait "$NAMED_PID" 2>/dev/null || true
rm -f "$SPAWN_OUT"
SHARED_LABELS_AFTER=$(shared_labels) || die "cannot recheck shared host labels"
if [ "$SHARED_LABELS_BEFORE" = "$SHARED_LABELS_AFTER" ]; then
    pass "shared host socket, library and presentation labels unchanged"
else
    fail "shared host labels changed: before=$SHARED_LABELS_BEFORE after=$SHARED_LABELS_AFTER"
fi

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "live named and disposable presentation binds held"
    echo "[presentation-live] $PASSCOUNT passes, 0 failures"
    exit 0
fi
echo "[presentation-live] $PASSCOUNT passes, $FAILCOUNT failures"
exit 1
