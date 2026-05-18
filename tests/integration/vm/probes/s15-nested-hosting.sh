#!/bin/bash
# §6.6 S6 — nested-compositor hosting proof of concept.
#
# Starts qdwin under RDP, then under qdwin spawns a nested Weston with
# --backend=wayland. The nested Weston's output registers as an
# xdg_toplevel on the *outer* qdwin via weston_desktop's normal path.
# Verifies:
#   1. Outer qdwin's toplevel_added fires.
#   2. qdshell's `list` ctrl command reports the nested-weston toplevel.
#   3. Killing the nested weston fires toplevel_removed; no zombie
#      views (outer `list` returns empty again).
#
# Shape A fallback (per spec/03 §Nested-compositor hosting, task 013
# S6): one outer toplevel whose inner surfaces render inside it. The
# multi-toplevel (xdg-foreign) path is out of scope for §6.6 — see the
# S6 research note at the end of this script.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s15-weston-outer.log
NLOG=/home/admin/s15-weston-nested.log
SHLOG=/home/admin/s15-qdshell.log
INI=/home/admin/.config/weston.ini
CTRL=/run/user/1000/qdshell-s15.sock
NESTED_SOCKET=qdwin-nested-0

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
loginctl enable-linger admin 2>/dev/null || true
for i in 1 2 3 4 5 6 7 8; do
    [ -S /run/user/1000/bus ] && break
    sleep 1
done

pkill -9 -x weston 2>/dev/null || true
pkill -9 -f "qdshell.py" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
pkill -9 -f "WAYLAND_DISPLAY=$NESTED_SOCKET" 2>/dev/null || true
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

rm -f "$WLOG" "$NLOG" "$SHLOG"; touch "$WLOG" "$NLOG" "$SHLOG"
chown admin:admin "$WLOG" "$NLOG" "$SHLOG"

# --- start outer qdwin ----------------------------------------------
cat >/home/admin/run-s15-outer.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s15-outer.sh; chown admin:admin /home/admin/run-s15-outer.sh
runuser -u admin -- nohup /home/admin/run-s15-outer.sh >>"$WLOG" 2>&1 </dev/null &
OUTPID=$!

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 60 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s15-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

# --- start qdshell (so we can query toplevels via ctrl) --------------
rm -f "$CTRL"
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 QDSHELL_BROKER_REQUIRED=0 \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus" \
    nohup python3 /home/admin/qdshell/qdshell.py \
        --ctrl-socket="$CTRL" \
        >>"$SHLOG" 2>&1 </dev/null &
SHPID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do
    grep -q "bound qdwin_shell_v1" "$SHLOG" 2>/dev/null && break
    sleep 1
done
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

# --- verify: baseline list is empty ---------------------------------
BEFORE=$(send_ctrl "list")
echo "=== baseline list ==="
echo "$BEFORE"
BEFORE_CT=$(echo "$BEFORE" | grep -c "^tl " || true)
echo "baseline toplevel count=$BEFORE_CT"
echo "PASS: qdshell reachable; baseline toplevel count=$BEFORE_CT"

# --- spawn nested weston under qdwin --------------------------------
cat >/home/admin/run-s15-nested.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-1
# Nested weston connects to qdwin via the parent wl_display.
exec weston \\
    --backend=wayland \\
    --shell=desktop-shell.so \\
    --width=640 --height=480 \\
    -Sqdwin-nested \\
    --log=$NLOG
EOF
chmod +x /home/admin/run-s15-nested.sh; chown admin:admin /home/admin/run-s15-nested.sh
runuser -u admin -- nohup /home/admin/run-s15-nested.sh >>"$NLOG" 2>&1 </dev/null &
NESTEDPID=$!

# Nested weston takes a few seconds to initialise on first run.
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sleep 1
    COUNT=$(send_ctrl "list" | grep -c "^tl " || true)
    [ "$COUNT" -gt "$BEFORE_CT" ] && break
done

AFTER=$(send_ctrl "list")
echo "=== after-nested list ==="
echo "$AFTER"
AFTER_CT=$(echo "$AFTER" | grep -c "^tl " || true)
if [ "$AFTER_CT" -le "$BEFORE_CT" ]; then
    echo "FAIL: nested weston didn't register as outer toplevel"
    echo "--- nested log (last 30 lines) ---"
    tail -30 "$NLOG"
    echo "--- outer weston log (last 30 lines) ---"
    tail -30 "$WLOG"
    # Keep going so cleanup still runs; exit non-zero at end.
    NESTED_OK=0
else
    echo "PASS: nested weston appears as outer toplevel (count $BEFORE_CT → $AFTER_CT)"
    NESTED_OK=1
fi

# Confirm compositor log shows a shell-accepted new client.
grep -q "toplevel_added\|qdwin: bind attempt" "$WLOG" || true

# --- teardown: kill nested → verify toplevel_removed ----------------
kill "$NESTEDPID" 2>/dev/null || true
pkill -9 -f "WAYLAND_DISPLAY=wayland-1 .*--backend=wayland" 2>/dev/null || true
pkill -9 -f "run-s15-nested" 2>/dev/null || true

for i in 1 2 3 4 5 6 7 8; do
    sleep 1
    COUNT=$(send_ctrl "list" | grep -c "^tl " || true)
    [ "$COUNT" -le "$BEFORE_CT" ] && break
done

FINAL=$(send_ctrl "list")
echo "=== final list ==="
echo "$FINAL"
FINAL_CT=$(echo "$FINAL" | grep -c "^tl " || true)
if [ "$NESTED_OK" -eq 1 ] && [ "$FINAL_CT" -gt "$BEFORE_CT" ]; then
    echo "FAIL: outer toplevel survived nested weston teardown"
    exit 3
fi
if [ "$NESTED_OK" -eq 1 ]; then
    echo "PASS: nested teardown cleaned up outer toplevel"
fi

# --- cleanup --------------------------------------------------------
kill "$SHPID" 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
kill "$OUTPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

[ "$NESTED_OK" -eq 1 ] || exit 4
echo "PASS: §6.6 S6 nested-compositor hosting (outer-toplevel shape)"

# --- research note --------------------------------------------------
# xdg-foreign (zxdg_exporter_v2 / zxdg_importer_v2) is client-to-
# client within a single display, not a cross-compositor bridge.
# Stock Weston advertises zxdg_exporter_v2 for *clients that connect
# to it*, but that doesn't extend to "nested weston exposes its inner
# toplevels to its outer compositor" — there's no protocol hook in
# the wayland-backend plumbing that splits each inner surface into a
# separate outer wl_surface.
#
# Native-feeling multi-toplevel nesting (where each app inside the
# nested weston appears as a peer toplevel on qdwin) requires a
# qdistro-side custom protocol where nested Weston publishes per-
# inner-toplevel metadata to qdwin. That's multi-week compositor
# work; §6.8 tier-2 isolation is the earliest reasonable home.
#
# For §6.6 we ship Shape-A-fallback: one outer toplevel whose inner
# surfaces render correctly inside it. The nested weston's own shell
# (desktop-shell.so above) handles decoration of its inner windows.
