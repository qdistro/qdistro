#!/bin/bash
# §6.6 S2 — notifications end-to-end smoke.
#
# Brings up weston + qdwin + qdshell (with the notifications daemon
# claiming org.freedesktop.Notifications on the session bus), then
# uses gdbus to call Notify() with a summary + body, verifies:
#   1. qdshell logs the bubble attach.
#   2. qdwin logs attach_notification.
#   3. ctrl-socket `notifications` shows one bubble.
#
# Then sleeps past the expire_timeout and re-queries the ctrl-socket
# to confirm the bubble expired.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s11-weston.log
SHLOG=/home/admin/s11-qdshell.log
INI=/home/admin/.config/weston.ini
CTRL=/run/user/1000/qdshell-s11.sock

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }

# Ensure admin's user session bus is up. Fresh VM has no logged-in
# session, so enable lingering to spawn user@1000 + dbus-broker.
loginctl enable-linger admin 2>/dev/null || true
for i in 1 2 3 4 5 6 7 8; do
    [ -S /run/user/1000/bus ] && break
    sleep 1
done
[ -S /run/user/1000/bus ] || {
    echo "ERROR: admin's session bus not reachable"; exit 1
}

pkill -9 -x weston 2>/dev/null || true
pkill -9 -f "qdshell.py" 2>/dev/null || true
pkill -9 -x s9-primary-selection 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
sleep 1

rm -rf /home/admin/qdshell
install -d -o admin -g admin /home/admin/qdshell
cp -r "$QDWIN_SRC/qdshell/." /home/admin/qdshell/
install -m 0644 "$QDWIN_SRC/qdwin/qdwin-shell-v1.xml" \
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

cat >/home/admin/run-s11-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s11-weston.sh; chown admin:admin /home/admin/run-s11-weston.sh
runuser -u admin -- nohup /home/admin/run-s11-weston.sh >>"$WLOG" 2>&1 </dev/null &

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 60 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s11-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

rm -f "$CTRL"
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 QDSHELL_BROKER_REQUIRED=0 \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus" \
    nohup python3 /home/admin/qdshell/qdshell.py \
        --ctrl-socket="$CTRL" \
        >>"$SHLOG" 2>&1 </dev/null &
SHPID=$!
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    grep -q "notifications installed" "$SHLOG" 2>/dev/null && break
    sleep 1
done
grep -q "notifications installed" "$SHLOG" || {
    echo "FAIL: notifications daemon not installed"
    tail -30 "$SHLOG"; exit 2
}
grep -q "daemon=True" "$SHLOG" || {
    echo "FAIL: notifications bus service not claimed"
    tail -20 "$SHLOG"; exit 3
}
echo "PASS: notifications daemon claimed org.freedesktop.Notifications"

# Fire a Notify. Use gdbus — already on the VM via glib2.
chmod a+rw "$CTRL" 2>/dev/null || true
runuser -u admin -- env DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus" \
    gdbus call --session \
        --dest org.freedesktop.Notifications \
        --object-path /org/freedesktop/Notifications \
        --method org.freedesktop.Notifications.Notify \
        qdtest 0 "" "Hello" "Body text from s11" "[]" "{}" 3000

sleep 1
grep -q "attach_notification anchor=0" "$WLOG" || {
    echo "FAIL: weston log missing attach_notification"
    tail -20 "$WLOG"; exit 4
}
echo "PASS: attach_notification reached compositor"

# Query ctrl-socket for the bubble.
OUT=$(runuser -u admin -- python3 -c '
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("'"$CTRL"'")
s.sendall(b"notifications\n")
data = b""
while b"ok notifications" not in data:
    chunk = s.recv(4096)
    if not chunk: break
    data += chunk
print(data.decode().strip())
')
echo "bubbles: $OUT"
echo "$OUT" | grep -q "summary='Hello'" || {
    echo "FAIL: expected Hello bubble"; exit 5
}
echo "$OUT" | grep -q "ok notifications count=1" || {
    echo "FAIL: expected exactly 1 bubble"; exit 6
}
echo "PASS: bubble visible via ctrl-socket"

# Wait past expire_timeout (3s + tick slack).
sleep 5
OUT=$(runuser -u admin -- python3 -c '
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("'"$CTRL"'")
s.sendall(b"notifications\n")
data = b""
while b"ok notifications" not in data:
    chunk = s.recv(4096)
    if not chunk: break
    data += chunk
print(data.decode().strip())
')
echo "bubbles (post-expire): $OUT"
echo "$OUT" | grep -q "ok notifications count=0" || {
    echo "FAIL: bubble did not expire"; exit 7
}
echo "PASS: bubble expired after 3s"

# Tray smoke: register a dummy SNI item, expect tray to list it.
runuser -u admin -- env DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus" \
    gdbus call --session \
        --dest org.kde.StatusNotifierWatcher \
        --object-path /StatusNotifierWatcher \
        --method org.kde.StatusNotifierWatcher.RegisterStatusNotifierItem \
        "/StatusNotifierItem/dummy" 2>&1 | head
sleep 1
OUT=$(runuser -u admin -- python3 -c '
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("'"$CTRL"'")
s.sendall(b"tray\n")
data = b""
while b"ok tray" not in data:
    chunk = s.recv(4096)
    if not chunk: break
    data += chunk
print(data.decode().strip())
')
echo "tray: $OUT"
echo "$OUT" | grep -q "watcher=yes" || {
    echo "FAIL: watcher not active"; exit 8
}
echo "$OUT" | grep -q "item " || {
    echo "FAIL: expected at least one tray item"; exit 9
}
echo "PASS: SNI watcher accepts RegisterStatusNotifierItem"

# Teardown.
kill "$SHPID" 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

echo "PASS: §6.6 S2 notifications + tray end-to-end"
