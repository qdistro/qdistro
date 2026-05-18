#!/bin/bash
# §6.6 S5 (draft) — locker end-to-end smoke.
#
# Drives lock/unlock via qdshell's ctrl-socket; verifies:
#   1. qdshell installs the locker module.
#   2. `lock` command: attach_lock_surface + set_locked=1 land in
#      weston log; locked_changed event logged in qdshell.
#   3. `unlock` command: set_locked=0 lands; locked_changed(0) logged.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s13-weston.log
SHLOG=/home/admin/s13-qdshell.log
INI=/home/admin/.config/weston.ini
CTRL=/run/user/1000/qdshell-s13.sock

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
loginctl enable-linger admin 2>/dev/null || true
for i in 1 2 3 4 5 6 7 8; do
    [ -S /run/user/1000/bus ] && break
    sleep 1
done

pkill -9 -x weston 2>/dev/null || true
pkill -9 -f "qdshell.py" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
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

rm -f "$WLOG" "$SHLOG"; touch "$WLOG" "$SHLOG"
chown admin:admin "$WLOG" "$SHLOG"

cat >/home/admin/run-s13-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s13-weston.sh; chown admin:admin /home/admin/run-s13-weston.sh
runuser -u admin -- nohup /home/admin/run-s13-weston.sh >>"$WLOG" 2>&1 </dev/null &

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 45 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s13-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

rm -f "$CTRL"
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 QDSHELL_BROKER_REQUIRED=0 \
    QDSHELL_LOCK_TEST=1 \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus" \
    nohup python3 /home/admin/qdshell/qdshell.py \
        --ctrl-socket="$CTRL" \
        >>"$SHLOG" 2>&1 </dev/null &
SHPID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do
    grep -q "locker installed" "$SHLOG" 2>/dev/null && break
    sleep 1
done
grep -q "locker installed" "$SHLOG" || {
    echo "FAIL: locker not installed"
    tail -20 "$SHLOG"; exit 2
}
echo "PASS: locker module installed"

chmod a+rw "$CTRL" 2>/dev/null || true

send_ctrl() {
    runuser -u admin -- python3 -c '
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("'"$CTRL"'")
s.sendall(("'"$1"'\n").encode())
data = b""
while True:
    c = s.recv(4096)
    if not c: break
    data += c
    if b"ok " in data or b"err " in data: break
print(data.decode().strip())
'
}

OUT=$(send_ctrl "lock")
echo "lock: $OUT"
sleep 1
grep -q "attach_lock_surface" "$WLOG" || {
    echo "FAIL: compositor log missing attach_lock_surface"
    tail -10 "$WLOG"; exit 3
}
grep -q "set_locked=1" "$WLOG" || {
    echo "FAIL: compositor log missing set_locked=1"
    tail -10 "$WLOG"; exit 4
}
echo "PASS: lock drove attach_lock_surface + set_locked=1"

OUT=$(send_ctrl "locker")
echo "locker: $OUT"
echo "$OUT" | grep -q "locked=True" || {
    echo "FAIL: locker state not locked"; exit 5
}
echo "PASS: locker state reports locked"

OUT=$(send_ctrl "unlock")
echo "unlock: $OUT"
sleep 1
grep -q "set_locked=0" "$WLOG" || {
    echo "FAIL: compositor log missing set_locked=0"
    tail -10 "$WLOG"; exit 6
}
echo "PASS: unlock drove set_locked=0"

OUT=$(send_ctrl "locker")
echo "$OUT" | grep -q "locked=False" || {
    echo "FAIL: locker still reports locked after unlock"; exit 7
}
echo "PASS: locker unlocked"

kill "$SHPID" 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

echo "PASS: §6.6 S5 locker end-to-end"
