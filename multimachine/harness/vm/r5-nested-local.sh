#!/bin/bash
# R5: one-machine, production-path nested-local liveness gate.
set -euo pipefail

RT=/run/mm-r5-local
XRT=/run/user/1000
OUTER=r5-outer
INNER=r5-inner
QDSHELL=/tmp/qdshell-r5
WLOG=$RT/outer.log
NLOG=$RT/inner.log
SLOG=$RT/qdshell.log
APPLOG=$RT/app.log
SHOT=$RT/proxy.png
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
    echo "--- outer tail ---"; tail -40 "$WLOG" 2>/dev/null || true
    echo "--- inner tail ---"; tail -40 "$NLOG" 2>/dev/null || true
    echo "--- qdshell mm/nested tail ---"
    grep -E 'NESTED|nested|pixelfeed|Qdwin' "$SLOG" 2>/dev/null | tail -40 || true
    exit 1
}

wait_log() {
    local file=$1 pattern=$2 label=$3
    for _ in $(seq 1 100); do
        grep -qE "$pattern" "$file" 2>/dev/null && return 0
        sleep 0.2
    done
    fail "timed out waiting for $label ($pattern in $file)"
}

command -v weston >/dev/null || fail "weston missing"
command -v qs >/dev/null || fail "qs missing"
command -v qdistro-nested-pixelfeed >/dev/null || fail "pixelfeed missing"
[ -x /tmp/r5-popup-probe ] || fail "staged popup probe missing"
command -v weston-screenshooter >/dev/null || fail "screenshooter missing"
[ -f "$QDSHELL/shell.qml" ] || fail "staged production qdshell missing"
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
idle-time=0
[shell]
locking=false
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
    QDWIN_ENABLE_SCREENSHOOTER=1 QDWIN_NESTED_BROKER_REQUIRED=1 \
    QDWIN_NESTED_S3D_TEST=1 \
    weston --backend=headless --renderer=pixman --debug \
      --width=1024 --height=640 --config="$RT/outer.ini" \
      --socket=$OUTER --log="$WLOG" &
OUTER_PID=$!
wait_log "$WLOG" 'qdwin: shell loaded' 'outer qdwin startup'
[ -S "$XRT/$OUTER" ] || fail "outer Wayland socket missing"
echo "PASS: outer qdwin started on a headless local output"

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=$XRT \
    WAYLAND_DISPLAY=$OUTER XDG_SESSION_TYPE=wayland \
    QML_DISABLE_DISK_CACHE=1 QML_IMPORT_PATH=/usr/share/qdistro/qml \
    /usr/bin/qs -p "$QDSHELL" --no-color -vv >"$SLOG" 2>&1 &
SHELL_PID=$!
wait_log "$WLOG" 'shell bound \(uid=1000 ' 'production qdshell bind'
echo "PASS: production qdshell owns the outer shell role"

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=$XRT \
    WAYLAND_DISPLAY=$OUTER QDWIN_OUTER_DISPLAY=$OUTER QDWIN_NESTED_MODE=1 \
    QDWIN_ALLOWED_UID=1000 \
    weston --config="$RT/inner.ini" --socket=$INNER --log="$NLOG" &
INNER_PID=$!
wait_log "$NLOG" 'nested-mode publisher ready' 'inner publisher startup'
echo "PASS: inner qdwin publisher bound qdwin_nested_v1 locally"

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=$XRT \
    WAYLAND_DISPLAY=$INNER /tmp/r5-popup-probe \
    --parent-w 400 --parent-h 300 --popup-w 180 --popup-h 120 \
    --offset-x 100000 --offset-y 100000 --hold-seconds 120 \
    >"$APPLOG" 2>&1 &
APP_PID=$!

wait_log "$WLOG" 'nested-proxy: created handle=[0-9]+.*pending=1' 'pending outer proxy'
HANDLE=$(sed -n 's/.*nested-proxy: created handle=\([0-9][0-9]*\).*/\1/p' "$WLOG" | head -1)
[ -n "$HANDLE" ] || fail "could not parse nested proxy handle"
wait_log "$SLOG" 'NESTED_PROXY_GATE.*verdict=allow' 'broker-backed qdshell allow'
wait_log "$WLOG" "holding_released handle=$HANDLE" 'proxy release after allow'
wait_log "$SLOG" "spawning pixelfeed for handle $HANDLE" 'production pixelfeed launch'
wait_log "$WLOG" "bind_proxy_pixels handle=$HANDLE.*(curtain swapped|deferred swap on allow)" 'pixel surface activation'
echo "PASS: one inner toplevel became one broker-approved outer proxy handle=$HANDLE"
echo "PASS: production pixelfeed bound a pixel surface to the proxy"

wait_log "$WLOG" "S3d route-test handle=$HANDLE.*pick_matched=1 active_input_proxy_matched=1" 'real proxy picker route'
wait_log "$NLOG" 'qdwin/nested: button handle=[0-9]+ btn=0x110 state=1' 'inner QDNI button press'
wait_log "$NLOG" 'qdwin/nested: button handle=[0-9]+ btn=0x110 state=0' 'inner QDNI button release'
echo "PASS: real outer picker routed a per-proxy QDNI button into inner qdwin"

mkdir -p "$RT/shots"; chown admin:admin "$RT/shots"
runuser -u admin -- bash -c "cd '$RT/shots' && XDG_RUNTIME_DIR='$XRT' WAYLAND_DISPLAY='$OUTER' weston-screenshooter >/dev/null 2>&1"
FRESH=$(ls -1t "$RT"/shots/wayland-screenshot-*.png 2>/dev/null | head -1)
[ -n "$FRESH" ] || fail "screenshooter produced no framebuffer"
mv "$FRESH" "$SHOT"; chown admin:admin "$SHOT"
echo "PASS: captured outer framebuffer at $SHOT"

runuser -u admin -- env XDG_RUNTIME_DIR=$XRT WAYLAND_DISPLAY=$OUTER \
    qs -p "$QDSHELL" ipc call qdwin closeWindow "$HANDLE" >/dev/null
wait_log "$WLOG" "request_close handle=$HANDLE .*fired close_requested" 'outer close request'
wait_log "$NLOG" 'qdwin/nested: outer close_requested handle=' 'inner close delivery'
sleep 1
kill -0 "$APP_PID" 2>/dev/null || fail "inner app died despite ignoring xdg close"
grep -q "nested-proxy: destroy handle=$HANDLE" "$WLOG" 2>/dev/null \
    && fail "outer destroyed proxy before inner owner released it"
echo "PASS: ignored outer close left inner app and proxy alive"

kill "$APP_PID"; wait "$APP_PID" 2>/dev/null || true; APP_PID=
wait_log "$WLOG" "nested-proxy: destroy handle=$HANDLE" 'source-owned proxy teardown'
kill -0 "$INNER_PID" 2>/dev/null || fail "inner compositor died with its app"
echo "PASS: inner owner destruction removed only its outer proxy"
echo "PASS: R5 nested-local liveness production gate"
