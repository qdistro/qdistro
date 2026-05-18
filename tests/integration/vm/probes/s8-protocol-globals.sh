#!/bin/bash
# §6.7 protocol-coverage driver: boots weston+qdwin, runs the
# pywayland probe against the four new globals
# (idle-inhibit / ext-idle-notify / cursor-shape / fractional-scale).
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s8-weston.log
PLOG=/home/admin/s8-probe.log
INI=/home/admin/.config/weston.ini

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
pkill -9 -x weston 2>/dev/null || true
pkill -9 -f "qdshell.py" 2>/dev/null || true
pkill -9 -f "s8-protocol-globals-probe.py" 2>/dev/null || true
sleep 1

rm -rf /home/admin/qdshell
install -d -o admin -g admin /home/admin/qdshell
cp -r "$QDWIN_SRC/qdshell/." /home/admin/qdshell/
install -m 0644 "$QDWIN_SRC/qdwin/qdwin/qdwin-shell-v1.xml" \
    /home/admin/qdshell/qdwin-shell-v1.xml
chown -R admin:admin /home/admin/qdshell
runuser -u admin -- env QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    /home/admin/qdshell/gen_protocol.sh >/dev/null

install -d -o admin -g admin /home/admin/.config
cat >"$INI" <<EOF
[core]
shell=qdwin-shell.so
# rdp-backend creates a wl_seat on peer-connect (needed by
# ext-idle-notify's get_idle_notification(seat) and by cursor-shape's
# get_pointer). We spawn a short-lived sdl-freerdp below to trigger
# seat creation before the probe runs.
backend=rdp-backend.so
require-outputs=any
idle-time=0

[shell]
locking=false

[output]
name=rdp
mode=1280x720
EOF
chown admin:admin "$INI"

rm -f "$WLOG" "$PLOG"; touch "$WLOG" "$PLOG"; chown admin:admin "$WLOG" "$PLOG"

cat >/home/admin/run-s8-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s8-weston.sh; chown admin:admin /home/admin/run-s8-weston.sh

runuser -u admin -- nohup /home/admin/run-s8-weston.sh >>"$WLOG" 2>&1 </dev/null &

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done

chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

install -m 0644 /root/s8-protocol-globals-probe.py \
    /home/admin/s8-protocol-globals-probe.py
chown admin:admin /home/admin/s8-protocol-globals-probe.py

# rdp-backend doesn't make a seat until a peer connects. Spawn a
# short sdl-freerdp just long enough for weston to register the seat.
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 20 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s8-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

set +e
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    QDSHELL_PROTO_DIR=/home/admin/qdshell \
    python3 /home/admin/s8-protocol-globals-probe.py 2>&1 | tee "$PLOG"
PROBE_RC=${PIPESTATUS[0]}
set -e

kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true

echo
echo "=== weston log (§6.7 traces) ==="
grep -E "idle-inhibit|cursor-shape" "$WLOG" || echo "(none)"

if [ "$PROBE_RC" -ne 0 ]; then
    echo "FAIL: probe exited $PROBE_RC"; exit "$PROBE_RC"
fi
grep -q "qdwin: idle-inhibit created" "$WLOG" || {
    echo "FAIL: compositor log missing 'idle-inhibit created'"; exit 4
}
grep -q "qdwin: cursor-shape set_shape=text" "$WLOG" || {
    echo "FAIL: compositor log missing 'cursor-shape set_shape=text'"; exit 5
}
echo "PASS: §6.7 protocol globals end-to-end"
