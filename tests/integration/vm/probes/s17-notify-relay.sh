#!/bin/bash
# §6.6 follow-up — per-uid notification relay.
# Admin's qdshell claims com.qdistro.Notifications1 on the system bus;
# any uid can call Notify. This test:
#   1. Installs the policy file + helper from /root/qdistro-src/deploy.
#   2. Starts qdwin + qdshell under admin (admin uid).
#   3. As root, calls qdistro-notify-send → expects bubble in admin's
#      compositor (seen via ctrl-socket `notifications`).
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s17-weston.log
SHLOG=/home/admin/s17-qdshell.log
INI=/home/admin/.config/weston.ini
CTRL=/run/user/1000/qdshell-s17.sock

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

# Ensure the system-bus policy is installed (s16 also does this; this
# script stands alone so re-install idempotently here too).
install -m 0644 "$QDWIN_SRC/deploy/com.qdistro.Notifications1.conf" \
    /usr/share/dbus-1/system.d/com.qdistro.Notifications1.conf
install -m 0755 "$QDWIN_SRC/deploy/qdistro-notify-send.py" \
    /usr/local/bin/qdistro-notify-send
pkill -HUP -x dbus-daemon 2>/dev/null || true
systemctl reload dbus.service 2>/dev/null || true
# dbus-broker honours /usr/share/dbus-1/system.d automatically after
# reload. Tumbleweed uses dbus-broker by default.

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

cat >/home/admin/run-s17-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston --rdp-tls-cert=$CERTDIR/rdp.crt \\
            --rdp-tls-key=$CERTDIR/rdp.key \\
            --log=$WLOG
EOF
chmod +x /home/admin/run-s17-weston.sh; chown admin:admin /home/admin/run-s17-weston.sh
runuser -u admin -- nohup /home/admin/run-s17-weston.sh >>"$WLOG" 2>&1 </dev/null &

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 60 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s17-sdl-freerdp.log 2>&1 </dev/null &
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
for i in 1 2 3 4 5 6 7 8 9 10; do
    grep -q "notifications installed" "$SHLOG" 2>/dev/null && break
    sleep 1
done
grep -q "notifications installed" "$SHLOG" || {
    echo "FAIL: notifications module not installed"
    tail -20 "$SHLOG"; exit 2
}
echo "PASS: notifications module installed"

# Wait until the system-relay name is claimed. Silent failure =
# admin's notifications didn't own com.qdistro.Notifications1.
for i in 1 2 3 4 5 6 7 8 9 10; do
    RELAY_OWNER=$(dbus-send --system --dest=org.freedesktop.DBus \
        --type=method_call --print-reply \
        /org/freedesktop/DBus \
        org.freedesktop.DBus.GetNameOwner \
        string:"com.qdistro.Notifications1" 2>/dev/null | grep -oE ":[0-9]+\.[0-9]+" || true)
    [ -n "$RELAY_OWNER" ] && break
    sleep 1
done
if [ -z "$RELAY_OWNER" ]; then
    echo "FAIL: com.qdistro.Notifications1 not claimed on system bus"
    # Dump the last few lines of qdshell log for diagnostics.
    tail -20 "$SHLOG"
    exit 3
fi
echo "PASS: system-bus relay owned by $RELAY_OWNER"

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

# Call the relay from root (simulating a user-uid app on a different
# uid than admin). Expect a bubble to appear in admin's compositor.
NID=$(/usr/local/bin/qdistro-notify-send \
    --app=smoke --urgency=1 "s17 relay test" "bubble from root uid") || {
    echo "FAIL: qdistro-notify-send exit=$?"
    exit 4
}
echo "relay returned nid=$NID"
sleep 1

OUT=$(send_ctrl "notifications")
echo "notifications: $OUT"
if echo "$OUT" | grep -q "s17 relay test"; then
    echo "PASS: relay bubble reached admin's compositor"
else
    echo "FAIL: relay bubble not seen"
    exit 5
fi

kill "$SHPID" 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

echo "PASS: §6.6 per-uid notification relay end-to-end"
