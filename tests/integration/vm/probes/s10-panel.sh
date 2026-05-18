#!/bin/bash
# §6.6 S1 — panel end-to-end smoke.
#
# Starts weston + qdwin + qdshell, waits for qdshell to call
# attach_panel (logged on both sides), queries qdshell's ctrl-socket
# for the panel geometry snapshot, then spawns a weston-terminal and
# verifies the maximised rect excludes the panel's 32 px zone.
#
# Run from inside the VM (path pattern matches other spike-6.5 probes).
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdwin-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s10-weston.log
SHLOG=/home/admin/s10-qdshell.log
INI=/home/admin/.config/weston.ini
CTRL=/run/user/1000/qdshell-s10.sock

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }

pkill -9 -x weston 2>/dev/null || true
pkill -9 -f "qdshell.py" 2>/dev/null || true
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

cat >/home/admin/run-s10-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s10-weston.sh; chown admin:admin /home/admin/run-s10-weston.sh
runuser -u admin -- nohup /home/admin/run-s10-weston.sh >>"$WLOG" 2>&1 </dev/null &

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

# RDP peer so output/seat exist.
runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 60 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s10-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

# Start qdshell.
rm -f "$CTRL"
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 QDSHELL_BROKER_REQUIRED=0 \
    nohup python3 /home/admin/qdshell/qdshell.py \
        --ctrl-socket="$CTRL" \
        >>"$SHLOG" 2>&1 </dev/null &
SHPID=$!

for i in 1 2 3 4 5 6 7 8 9 10; do
    grep -q "panel installed" "$SHLOG" 2>/dev/null && break
    sleep 1
done
grep -q "attach_panel" "$WLOG" || {
    echo "FAIL: weston log missing attach_panel"
    tail -20 "$WLOG"; exit 4
}
echo "PASS: attach_panel wired end-to-end"

# Query panel geometry. RDP output size is SDL-peer-dependent (often
# 1024x768 from SDL dummy; could be other sizes on different peers).
# We just check shape (x=0, y=H-32, w=W, h=32) where W,H come from
# the weston output log.
sleep 1
chmod a+rw "$CTRL" 2>/dev/null || true
PANEL_LINE=$(runuser -u admin -- python3 -c '
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("'"$CTRL"'")
s.sendall(b"panel\n")
data = b""
while True:
    chunk = s.recv(4096)
    if not chunk: break
    data += chunk
    if b"\n" in data: break
print(data.decode().strip())
')
echo "qdshell panel: $PANEL_LINE"
echo "$PANEL_LINE" | grep -q "attached=yes" || {
    echo "FAIL: panel not attached"; exit 5
}
# Extract output size from weston log. rdp output logs "Head 'rdp':
# WxH" or "new resolution: WxH". Fall back on parsing the panel line.
OUT_W=$(awk '/ pos =[[:space:]]+0,[[:space:]]*0.*size =/{
    match($0, /size =[[:space:]]+([0-9]+)x([0-9]+)/, a);
    print a[1]; exit }
    /current mode[[:space:]]+[0-9]+x[0-9]+/{
    match($0, /([0-9]+)x([0-9]+)/, a); print a[1]; exit }
    /Head[[:space:]]+.rdp/{
    match($0, /([0-9]+)x([0-9]+)/, a); print a[1]; exit }' "$WLOG")
OUT_H=$(awk '/ pos =[[:space:]]+0,[[:space:]]*0.*size =/{
    match($0, /size =[[:space:]]+([0-9]+)x([0-9]+)/, a);
    print a[2]; exit }
    /current mode[[:space:]]+[0-9]+x[0-9]+/{
    match($0, /([0-9]+)x([0-9]+)/, a); print a[2]; exit }
    /Head[[:space:]]+.rdp/{
    match($0, /([0-9]+)x([0-9]+)/, a); print a[2]; exit }' "$WLOG")
# Fallback: parse geometry line directly (panel w = output_w).
if [ -z "$OUT_W" ] || [ -z "$OUT_H" ]; then
    OUT_W=$(echo "$PANEL_LINE" | sed -n 's/.*geom=[0-9]*,[0-9]*,\([0-9]*\),[0-9]*.*/\1/p')
    GEOM_Y=$(echo "$PANEL_LINE" | sed -n 's/.*geom=[0-9]*,\([0-9]*\),[0-9]*,[0-9]*.*/\1/p')
    GEOM_H=$(echo "$PANEL_LINE" | sed -n 's/.*geom=[0-9]*,[0-9]*,[0-9]*,\([0-9]*\).*/\1/p')
    OUT_H=$((GEOM_Y + GEOM_H))
fi
[ -n "$OUT_W" ] && [ -n "$OUT_H" ] || {
    echo "FAIL: could not determine output dimensions"; exit 6
}
EXPECTED="geom=0,$((OUT_H-32)),$OUT_W,32"
echo "output ${OUT_W}x${OUT_H}; expecting panel $EXPECTED"
echo "$PANEL_LINE" | grep -q "$EXPECTED" || {
    echo "FAIL: panel geometry mismatch, wanted $EXPECTED"; exit 6
}
echo "PASS: panel geometry = $EXPECTED"

# Spawn weston-terminal and maximise; verify it reports the work-area
# size, not the full output size.
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    nohup weston-terminal >/tmp/s10-term.log 2>&1 </dev/null &
for i in 1 2 3 4 5 6 7 8; do
    grep -q "toplevel_added" "$WLOG" 2>/dev/null && break
    sleep 1
done

# Find the toplevel handle from the ctrl socket.
HANDLE=$(runuser -u admin -- python3 -c '
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("'"$CTRL"'")
s.sendall(b"list\n")
data = b""
while b"ok list" not in data:
    chunk = s.recv(4096)
    if not chunk: break
    data += chunk
for line in data.decode().splitlines():
    if line.startswith("tl "):
        print(line.split()[1]); break
')
[ -n "$HANDLE" ] || { echo "FAIL: no toplevel handle"; exit 7; }
echo "terminal handle=$HANDLE"

runuser -u admin -- python3 -c '
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("'"$CTRL"'")
s.sendall(b"max '"$HANDLE"'\n")
print(s.recv(4096).decode().strip())
'

# Give the compositor a moment to re-apply and log.
sleep 1
# Expect outer=<out_w>x<out_h-32>  (output minus 32 px bottom panel).
EXPECTED_MAX="max=1 outer=${OUT_W}x$((OUT_H-32))"
grep -q "$EXPECTED_MAX" "$WLOG" || {
    echo "FAIL: expected '$EXPECTED_MAX' in weston log"
    grep "request_maximize" "$WLOG" | tail -5
    exit 8
}
echo "PASS: maximise respects 32px panel exclusive zone ($EXPECTED_MAX)"

# Teardown.
kill "$SHPID" 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

echo "PASS: §6.6 S1 panel end-to-end"
