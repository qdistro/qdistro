#!/bin/bash
# R9 source: one qdwin composition authority with a local headless output and
# one pre-created RDP output slot.  The driver mutates rdp-0 through qdwin's
# trusted wlr-output-management path; this process stays alive across cycles.
set -euo pipefail

RT=/run/mm-r9-source
XRT=/run/user/1000
SOCK=r9-source
W=${W:-1280}
H=${H:-800}
MW=${MW:-512}
MH=${MH:-400}
SEAM=${SEAM:-256}
GEN=${GEN:-90}
PORT=${PORT:-3389}
APP_PID=
WESTON_PID=

cleanup() {
    set +e
    [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null
    [ -n "$WESTON_PID" ] && kill "$WESTON_PID" 2>/dev/null
    rm -f "$XRT/$SOCK" "$XRT/$SOCK.lock"
}
trap cleanup EXIT

fail() {
    echo "FAIL: $*"
    tail -80 "$RT/weston.log" 2>/dev/null || true
    exit 1
}

run_admin() {
    runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=$XRT \
        WAYLAND_DISPLAY=$SOCK "$@"
}

for command in weston /tmp/r9-qdwin-output-probe /tmp/r9-qdwin-marker-client; do
    command -v "$command" >/dev/null 2>&1 || [ -x "$command" ] || \
        fail "missing $command"
done

systemctl stop mm-viewer-session mm-qdwin mm-seatd mm-ydotoold 2>/dev/null || true
runuser -u admin -- systemctl --user stop noctalia-session noctalia-shell \
    qdlocker qdshell.service 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true
pkill -9 -f r9-qdwin-marker-client 2>/dev/null || true
rm -rf "$RT"
install -d -o admin -g admin -m 0700 "$RT"
rm -f "$XRT/$SOCK" "$XRT/$SOCK.lock"

if [ ! -s /home/admin/qdwin-rdp/rdp.crt ] || \
   [ ! -s /home/admin/qdwin-rdp/rdp.key ]; then
    fail "RDP TLS material missing"
fi

cat >"$RT/weston.ini" <<EOF
[core]
shell=/tmp/r9-qdwin-shell.so
require-outputs=any
renderer=pixman
idle-time=0
[shell]
locking=false
[output]
name=headless
mode=${W}x${H}
[output]
name=rdp-0
mode=${W}x${H}
EOF
chown admin:admin "$RT/weston.ini"

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=$XRT \
    QDWIN_ALLOWED_UID=1000 QDWIN_ALLOWED_LOCKER_ANY=1 \
    QDWIN_ENABLE_SCREENSHOOTER=1 \
    QDWIN_TEST_PLACE_APPID=qdwin-marker-client \
    QDWIN_TEST_PLACE_X=$((W - SEAM)) QDWIN_TEST_PLACE_Y=200 \
    weston --backends=headless,rdp --renderer=pixman \
      --config="$RT/weston.ini" --width="$W" --height="$H" --port="$PORT" \
      --rdp-tls-cert=/home/admin/qdwin-rdp/rdp.crt \
      --rdp-tls-key=/home/admin/qdwin-rdp/rdp.key \
      --socket=$SOCK --log="$RT/weston.log" &
WESTON_PID=$!

for _ in $(seq 1 100); do
    [ -S "$XRT/$SOCK" ] && grep -q 'qdwin: shell loaded' "$RT/weston.log" && break
    sleep 0.2
done
[ -S "$XRT/$SOCK" ] || fail "source qdwin socket missing"
grep -q 'qdwin: shell loaded' "$RT/weston.log" || fail "qdwin shell not loaded"

# The backend creates rdp-0 at startup.  Product v1 reserves a bounded slot and
# makes it non-desktop until a generation-bound display lease is admitted.
run_admin /tmp/r9-qdwin-output-probe --apply --disable=rdp-0 \
    >"$RT/disable-initial.log" 2>&1 || fail "initial slot disable failed"
run_admin /tmp/r9-qdwin-output-probe --expect-heads=2 \
    --expect-state=rdp-0:0 >"$RT/initial-state.log" 2>&1 || \
    fail "rdp-0 did not become disabled"

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=$XRT \
    WAYLAND_DISPLAY=$SOCK /tmp/r9-qdwin-marker-client \
      --width "$MW" --height "$MH" --seam-x "$SEAM" \
      --output-id 9 --generation "$GEN" --frame 1 --animate-ms 200 \
      --telemetry "$RT/marker-telemetry.json" --label r9-straddle \
      >"$RT/marker.log" 2>&1 &
APP_PID=$!
echo "$APP_PID" >"$RT/app.pid"
echo "$WESTON_PID" >"$RT/weston.pid"
chown admin:admin "$RT/app.pid" "$RT/weston.pid"
sleep 1
kill -0 "$APP_PID" 2>/dev/null || fail "marker exited before attach"
touch "$RT/ready"
chown admin:admin "$RT/ready"
echo "R9_SOURCE_READY weston=$WESTON_PID app=$APP_PID"

while [ ! -e "$RT/stop" ]; do
    kill -0 "$WESTON_PID" 2>/dev/null || fail "source compositor exited"
    kill -0 "$APP_PID" 2>/dev/null || fail "source marker exited"
    sleep 0.2
done
