#!/bin/bash
# One real viewer qdwin/qdshell stack for two independent R7 streams.
set -euo pipefail

RT=/run/mm-r7-viewer
XRT=/run/user/1000
DISPLAY=r7-viewer
QDSHELL=/tmp/qdshell-r5
ACTION=qdistro.nested.advertise:qdwin-popup-probe
WESTON_PID=
SHELL_PID=

cleanup() {
    set +e
    [ -n "$SHELL_PID" ] && kill "$SHELL_PID" 2>/dev/null
    [ -n "$WESTON_PID" ] && kill "$WESTON_PID" 2>/dev/null
    rm -f "$XRT/$DISPLAY" "$XRT/$DISPLAY.lock"
}
trap cleanup EXIT

fail() {
    echo "FAIL: $*"
    tail -100 "$RT/weston.log" 2>/dev/null || true
    tail -100 "$RT/qdshell.log" 2>/dev/null || true
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

for command in weston qs qdistro-mm-remote-pixelfeed; do
    command -v "$command" >/dev/null || fail "missing $command"
done
[ -x /usr/bin/qdistro-mm-remote-viewer-helper ] || fail "viewer helper missing"
[ -f "$QDSHELL/shell.qml" ] || fail "staged qdshell missing"

systemctl stop mm-viewer-session mm-qdwin mm-seatd mm-ydotoold 2>/dev/null || true
runuser -u admin -- systemctl --user stop noctalia-session noctalia-shell qdlocker qdshell.service 2>/dev/null || true
pkill -9 -f '/usr/bin/qs -p /tmp/qdshell-r5' 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true
for _ in $(seq 1 50); do
    pgrep -x weston >/dev/null || break
    sleep 0.1
done

rm -rf "$RT"; install -d -o admin -g admin -m 0700 "$RT"
rm -f "$XRT/$DISPLAY" "$XRT/$DISPLAY.lock"
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

cat >"$RT/weston.ini" <<EOF
[core]
shell=/usr/lib64/weston/qdwin-shell.so
backend=headless-backend.so
require-outputs=any
renderer=pixman
idle-time=0
[shell]
locking=false
[output]
name=headless
mode=1024x640
EOF
chown admin:admin "$RT/weston.ini"

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=$XRT \
    QDWIN_ALLOWED_UID=1000 QDWIN_ALLOWED_LOCKER_ANY=1 \
    QDWIN_NESTED_BROKER_REQUIRED=1 QDWIN_NESTED_S3B_TEST=1 \
    weston --debug --width=1024 --height=640 --config="$RT/weston.ini" \
      --socket=$DISPLAY --log="$RT/weston.log" &
WESTON_PID=$!
wait_log "$RT/weston.log" 'qdwin: shell loaded' 'viewer qdwin'

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=$XRT \
    WAYLAND_DISPLAY=$DISPLAY XDG_SESSION_TYPE=wayland \
    QML_DISABLE_DISK_CACHE=1 QML_IMPORT_PATH=/usr/share/qdistro/qml \
    /usr/bin/qs -p "$QDSHELL" --no-color -vv >"$RT/qdshell.log" 2>&1 &
SHELL_PID=$!
wait_log "$RT/weston.log" 'shell bound \(uid=1000 ' 'viewer qdshell bind'
touch "$RT/ready"; chown admin:admin "$RT/ready"
echo "R7_VIEWER_STACK_READY"
while [ ! -e "$RT/stop" ]; do
    kill -0 "$WESTON_PID" 2>/dev/null || fail "viewer qdwin exited"
    sleep 0.2
done

