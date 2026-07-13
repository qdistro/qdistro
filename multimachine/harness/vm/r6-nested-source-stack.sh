#!/bin/bash
# Long-lived real source qdwin stack for the R6 two-VM product gate.
set -euo pipefail

RT=/run/mm-r6-source
XRT=/run/user/1000
OUTER=r6-source-outer
INNER=r6-source-inner
QDSHELL=/tmp/qdshell-r5
ACTION=qdistro.nested.advertise:qdwin-popup-probe
APP_PID=
INNER_PID=
OUTER_PID=
SHELL_PID=

cleanup() {
    set +e
    [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null
    [ -n "$SHELL_PID" ] && kill "$SHELL_PID" 2>/dev/null
    pkill -9 -f qdistro-nested-pixelfeed 2>/dev/null
    [ -n "$INNER_PID" ] && kill "$INNER_PID" 2>/dev/null
    [ -n "$OUTER_PID" ] && kill "$OUTER_PID" 2>/dev/null
    rm -f "$XRT/$OUTER" "$XRT/$OUTER.lock" "$XRT/$INNER" "$XRT/$INNER.lock"
}
trap cleanup EXIT

fail() {
    echo "FAIL: $*"
    tail -60 "$RT/outer.log" 2>/dev/null || true
    tail -60 "$RT/inner.log" 2>/dev/null || true
    tail -60 "$RT/qdshell.log" 2>/dev/null || true
    exit 1
}

wait_log() {
    local file=$1 pattern=$2 label=$3
    for _ in $(seq 1 150); do
        grep -qE "$pattern" "$file" 2>/dev/null && return 0
        sleep 0.2
    done
    fail "timed out waiting for $label"
}

for command in weston qs qdistro-nested-pixelfeed; do
    command -v "$command" >/dev/null || fail "missing $command"
done
[ -x /tmp/r5-popup-probe ] || fail "popup probe missing"
[ -f "$QDSHELL/shell.qml" ] || fail "staged qdshell missing"
pgrep -x pipewire >/dev/null || fail "admin PipeWire daemon missing"

systemctl stop mm-viewer-session mm-qdwin mm-seatd mm-ydotoold 2>/dev/null || true
runuser -u admin -- systemctl --user stop noctalia-session noctalia-shell qdlocker qdshell.service 2>/dev/null || true
pkill -9 -f '/usr/bin/qs -p /tmp/qdshell-r5' 2>/dev/null || true
pkill -9 -f qdistro-nested-pixelfeed 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true
for _ in $(seq 1 50); do
    pgrep -x weston >/dev/null || break
    sleep 0.1
done

rm -rf "$RT"; install -d -o admin -g admin -m 0700 "$RT"
rm -f "$XRT/$OUTER" "$XRT/$OUTER.lock" "$XRT/$INNER" "$XRT/$INNER.lock"
systemctl start qdistro-admin-broker.service
python3 - <<PY
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
for row in c.list_all():
    if row["caller_uid"] == 1000 and row["action"] == "$ACTION":
        c.delete_by_id(row["id"])
c.store(1000, "$ACTION", "", "forever", True, 1000)
PY

cat >"$RT/outer.ini" <<EOF
[core]
shell=/usr/lib64/weston/qdwin-shell.so
backend=rdp-backend.so
require-outputs=any
renderer=pixman
idle-time=0
[shell]
locking=false
[output]
name=rdp-0
mode=1024x640
EOF
cat >"$RT/inner.ini" <<EOF
[core]
shell=/usr/lib64/weston/qdwin-shell.so
backend=pipewire-backend.so
require-outputs=any
renderer=pixman
idle-time=0
[shell]
locking=false
[pipewire]
num-outputs=8
EOF
chown admin:admin "$RT/outer.ini" "$RT/inner.ini"

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=$XRT \
    QDWIN_ALLOWED_UID=1000 QDWIN_ALLOWED_LOCKER_ANY=1 \
    QDWIN_NESTED_BROKER_REQUIRED=1 \
    weston --debug --width=1024 --height=640 --config="$RT/outer.ini" \
      --rdp-tls-cert=/home/admin/qdwin-rdp/rdp.crt \
      --rdp-tls-key=/home/admin/qdwin-rdp/rdp.key \
      --socket=$OUTER --log="$RT/outer.log" &
OUTER_PID=$!
wait_log "$RT/outer.log" 'qdwin: shell loaded' 'outer qdwin'
wait_log "$RT/outer.log" "seat '" 'RDP seat'

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=$XRT \
    WAYLAND_DISPLAY=$OUTER XDG_SESSION_TYPE=wayland \
    QML_DISABLE_DISK_CACHE=1 QML_IMPORT_PATH=/usr/share/qdistro/qml \
    /usr/bin/qs -p "$QDSHELL" --no-color -vv >"$RT/qdshell.log" 2>&1 &
SHELL_PID=$!
wait_log "$RT/outer.log" 'shell bound \(uid=1000 ' 'qdshell bind'

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=$XRT \
    WAYLAND_DISPLAY=$OUTER QDWIN_OUTER_DISPLAY=$OUTER QDWIN_NESTED_MODE=1 \
    QDWIN_ALLOWED_UID=1000 \
    weston --config="$RT/inner.ini" --socket=$INNER --log="$RT/inner.log" &
INNER_PID=$!
wait_log "$RT/inner.log" 'nested-mode publisher ready' 'inner publisher'

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=$XRT \
    WAYLAND_DISPLAY=$INNER /tmp/r5-popup-probe \
    --parent-w 400 --parent-h 300 --popup-w 180 --popup-h 120 \
    --offset-x 40 --offset-y 50 --hold-seconds 600 \
    >"$RT/app.log" 2>&1 &
APP_PID=$!
echo "$APP_PID" >"$RT/app.pid"
chown admin:admin "$RT/app.pid"

wait_log "$RT/outer.log" 'nested-toplevel advertise pw_node=' 'source advertise'
wait_log "$RT/outer.log" 'holding_released handle=[0-9]+ via nested_proxy_decision/allow' 'broker allow'
touch "$RT/ready"; chown admin:admin "$RT/ready"
echo "R6_SOURCE_STACK_READY app=$APP_PID"
while [ ! -e "$RT/stop" ]; do
    kill -0 "$APP_PID" 2>/dev/null || break
    sleep 0.2
done
wait "$APP_PID" 2>/dev/null || true
APP_PID=
