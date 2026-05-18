#!/bin/bash
# §6.5 S3c iter3 gating verification: run weston + qdshell + subscribe,
# then WITHOUT connecting any RDP client, sit idle for 5s and observe
# that on_stream_process does NOT tick. If gating works, the pulse
# thread skips request_frame when ArrayList_Count(server->clients) == 0.
set -eo pipefail
WLOG=/home/admin/gate-weston.log
SLOG=/home/admin/gate-qdshell.log
SOCK=/tmp/qdshell-gate.sock
INI=/home/admin/.config/weston.ini
CERTDIR=/home/admin/qdwin-rdp
QDWIN_SRC=/root/qdistro-src

pgrep -x pipewire >/dev/null
pkill -9 -x weston 2>/dev/null || true
pkill -9 weston-terminal 2>/dev/null || true
pkill -9 -f qdshell.py 2>/dev/null || true
pkill -9 -f qdistro-forward 2>/dev/null || true
sleep 1
rm -f "$WLOG" "$SLOG"
touch "$WLOG" "$SLOG"; chown admin:admin "$WLOG" "$SLOG"

cat >"$INI" <<INI
[core]
shell=qdwin-shell.so
backend=rdp-backend.so,pipewire-backend.so
require-outputs=any
idle-time=0
[shell]
locking=false
[output]
name=rdp-0
mode=1280x720
[pipewire]
num-outputs=2
INI
chown -R admin:admin /home/admin/.config

rm -rf /home/admin/qdshell
install -d -o admin -g admin /home/admin/qdshell
cp -r "$QDWIN_SRC/qdshell/." /home/admin/qdshell/
install -m 0644 "$QDWIN_SRC/qdwin/qdwin/qdwin-shell-v1.xml" \
    /home/admin/qdshell/qdwin-shell-v1.xml
chown -R admin:admin /home/admin/qdshell
runuser -u admin -- env QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    /home/admin/qdshell/gen_protocol.sh >/dev/null

cat >/home/admin/gate-run.sh <<WEND
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston --rdp-tls-cert=$CERTDIR/rdp.crt --rdp-tls-key=$CERTDIR/rdp.key --log=$WLOG
WEND
chmod +x /home/admin/gate-run.sh
chown admin:admin /home/admin/gate-run.sh
runuser -u admin -- nohup /home/admin/gate-run.sh >>"$WLOG" 2>&1 </dev/null &
for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
    QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    QDSHELL_BROKER_REQUIRED=0 \
    nohup python3 /home/admin/qdshell/qdshell.py --ctrl-socket=$SOCK >>"$SLOG" 2>&1 </dev/null &
for i in 1 2 3 4 5; do [ -S "$SOCK" ] && break; sleep 1; done

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
    nohup weston-terminal >/dev/null 2>&1 </dev/null &
sleep 3

HANDLE=$(echo 'list' | socat - UNIX-CONNECT:$SOCK | awk '/^tl /{print $2; exit}')
echo "[gate] handle=$HANDLE"
echo "stream $HANDLE gate 640 480 0" | socat - UNIX-CONNECT:$SOCK
sleep 2

# Wait for pulse to start so we know the thread is up.
for i in 1 2 3 4; do
    grep -q 'frame_pulse thread started' "$WLOG" 2>/dev/null && break
    sleep 1
done

MARK1=$(grep -c 'on_stream_process tick' "$WLOG" || true)
echo "[gate] mark1=$MARK1 (after subscribe + pulse started, before quiet)"
sleep 5
MARK2=$(grep -c 'on_stream_process tick' "$WLOG" || true)
echo "[gate] mark2=$MARK2 (after 5s quiet with 0 RDP peers)"

DELTA=$((MARK2 - MARK1))
echo "[gate] delta=$DELTA ticks during 5s idle"

echo "stream-stop $HANDLE" | socat - UNIX-CONNECT:$SOCK
sleep 1

# on_stream_process only logs every 60th frame; one stray tick across
# 5s is tolerable. Anything >1 means the gate isn't working.
if [ "$DELTA" -le 1 ]; then
    echo "[gate] PASS — pulse correctly idle when no peer connected"
    exit 0
else
    echo "[gate] FAIL: pulse fired ~$((DELTA*60)) frames/5s despite no peer"
    exit 1
fi
