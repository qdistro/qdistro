#!/bin/bash
# §6.6 S3/S4 — launcher + switcher end-to-end smoke.
#
# Brings up weston + qdwin + qdshell; uses the ctrl-socket to toggle
# the launcher, set a filter, verify the match count, spawn, and
# then exercise the switcher via ctrl-socket `switcher-next` /
# `switcher-commit` (simulating Alt+Tab since real keyboard injection
# into a headless RDP session isn't practical).
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s12-weston.log
SHLOG=/home/admin/s12-qdshell.log
INI=/home/admin/.config/weston.ini
CTRL=/run/user/1000/qdshell-s12.sock

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

# Make sure the launcher has some visible entries to index. Minimal
# Tumbleweed VMs ship only a couple of NoDisplay=true .desktop files;
# we drop a couple of proper ones so the filter exercise is meaningful.
install -d -m 0755 /usr/share/applications
cat >/usr/share/applications/s12-weston-terminal.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Weston Terminal (s12)
Exec=weston-terminal
Terminal=false
Categories=System;
EOF
cat >/usr/share/applications/s12-xterm.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=XTerm (s12)
Exec=xterm
Terminal=false
Categories=System;
EOF

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

cat >/home/admin/run-s12-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s12-weston.sh; chown admin:admin /home/admin/run-s12-weston.sh
runuser -u admin -- nohup /home/admin/run-s12-weston.sh >>"$WLOG" 2>&1 </dev/null &

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 60 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s12-sdl-freerdp.log 2>&1 </dev/null &
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
    grep -q "launcher + switcher installed" "$SHLOG" 2>/dev/null && break
    sleep 1
done
grep -q "launcher + switcher installed" "$SHLOG" || {
    echo "FAIL: launcher not installed"
    tail -20 "$SHLOG"; exit 2
}
echo "PASS: launcher module installed"

chmod a+rw "$CTRL" 2>/dev/null || true
send_ctrl() {
    runuser -u admin -- python3 -c '
import socket, sys
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

# Toggle launcher on.
OUT=$(send_ctrl "launcher-toggle")
echo "toggle: $OUT"
echo "$OUT" | grep -q "visible=True" || {
    echo "FAIL: launcher not visible after toggle"; exit 3
}
sleep 1
grep -q "attach_launcher kind=0" "$WLOG" || {
    echo "FAIL: weston log missing attach_launcher kind=0"
    tail -10 "$WLOG"; exit 4
}
echo "PASS: launcher attached on toggle"

# Query snapshot.
OUT=$(send_ctrl "launcher")
echo "launcher: $OUT"
echo "$OUT" | grep -q "indexed=" || {
    echo "FAIL: no desktop entries indexed"; exit 5
}
IDX=$(echo "$OUT" | sed -n 's/.*indexed=\([0-9]*\).*/\1/p')
[ "$IDX" -gt 0 ] || { echo "FAIL: 0 entries indexed"; exit 6; }
echo "PASS: indexed $IDX desktop entries"

# Set filter.
OUT=$(send_ctrl "launcher-type term")
echo "filter: $OUT"
echo "$OUT" | grep -q "matches=" || {
    echo "FAIL: launcher-type didn't return matches"; exit 7
}
echo "PASS: filter applied"

# Toggle off.
OUT=$(send_ctrl "launcher-toggle")
echo "toggle off: $OUT"
echo "$OUT" | grep -q "visible=False" || {
    echo "FAIL: launcher still visible after second toggle"; exit 8
}
echo "PASS: launcher toggles off"

# Switcher: spawn two weston-terminals, cycle, commit.
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    nohup weston-terminal >/tmp/s12-term1.log 2>&1 </dev/null &
sleep 1
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    nohup weston-terminal >/tmp/s12-term2.log 2>&1 </dev/null &
for i in 1 2 3 4 5 6; do
    CNT=$(grep -c "toplevel_added" "$SHLOG" || true)
    [ "${CNT:-0}" -ge 2 ] && break
    sleep 1
done

OUT=$(send_ctrl "switcher-next")
echo "switcher-next: $OUT"
OUT=$(send_ctrl "switcher")
echo "switcher: $OUT"
echo "$OUT" | grep -q "visible=True" || {
    echo "FAIL: switcher not visible"; exit 9
}
echo "$OUT" | grep -q "count=2" || {
    echo "FAIL: switcher count != 2"; exit 10
}
grep -q "attach_launcher kind=1" "$WLOG" || {
    echo "FAIL: weston log missing attach_launcher kind=1"
    tail -10 "$WLOG"; exit 11
}
echo "PASS: switcher attached + cycling"

OUT=$(send_ctrl "switcher-commit")
echo "switcher-commit: $OUT"
sleep 1
OUT=$(send_ctrl "switcher")
echo "$OUT" | grep -q "visible=False" || {
    echo "FAIL: switcher still visible after commit"; exit 12
}
echo "PASS: switcher dismissed on commit"

# Teardown.
kill "$SHPID" 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

echo "PASS: §6.6 S3/S4 launcher + switcher end-to-end"
