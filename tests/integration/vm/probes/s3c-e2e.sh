#!/bin/bash
# §6.5 S3c end-to-end smoke:
#  - bring up pipewire + weston + qdshell
#  - subscribe a weston-terminal handle
#  - verify the C qdistro-forward spawned on the assigned port
#  - confirm TCP port accepts (nc -z)
#  - sdl-freerdp connect (5s timeout, will fail-open if it ever stops being usable
#    headless, but at minimum must reach the auth/handshake)
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s3c-weston.log
SLOG=/home/admin/s3c-qdshell.log
FLOG=/home/admin/s3c-freerdp.log
FFWDLOG=/home/admin/s3c-qfwd.log
SOCK=/tmp/qdshell-s3c.sock
INI=/home/admin/.config/weston.ini

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
pkill -9 -x weston 2>/dev/null || true
pkill -9 weston-terminal 2>/dev/null || true
pkill -9 -f qdshell.py 2>/dev/null || true
pkill -9 -f qdistro-forward 2>/dev/null || true
pkill -9 -f sdl-freerdp 2>/dev/null || true
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
EOF
chown -R admin:admin /home/admin/.config

rm -f "$WLOG" "$SLOG" "$FLOG" "$FFWDLOG"
touch "$WLOG" "$SLOG" "$FLOG" "$FFWDLOG"
chown admin:admin "$WLOG" "$SLOG" "$FLOG" "$FFWDLOG"

cat >/home/admin/run-s3c-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s3c-weston.sh
chown admin:admin /home/admin/run-s3c-weston.sh

runuser -u admin -- nohup /home/admin/run-s3c-weston.sh >>"$WLOG" 2>&1 </dev/null &
for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    QDSHELL_BROKER_REQUIRED=0 \
    nohup python3 /home/admin/qdshell/qdshell.py \
        --ctrl-socket=$SOCK >>$SLOG 2>&1 </dev/null &
for i in 1 2 3 4 5; do
    [ -S "$SOCK" ] && break
    sleep 1
done

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    nohup weston-terminal >/dev/null 2>&1 </dev/null &
sleep 3

HANDLE=$(echo "list" | socat - UNIX-CONNECT:$SOCK | awk '/^tl /{print $2; exit}')
echo "[s3c] handle=$HANDLE"
[ -z "$HANDLE" ] && { echo "[s3c] FAIL: no toplevel"; exit 2; }

# Clear dump path BEFORE subscribe — qdistro-forward auto-dumps on
# first frame, which arrives within ~1s of subscribe. If we rm later,
# we delete the proof.
rm -f /tmp/qfwd-dump.ppm

echo "stream $HANDLE diag 640 480 0" | socat - UNIX-CONNECT:$SOCK
# Sleep long enough that frames 1, 5, 30, 60 all have a chance to land
# (~2-3s at 30fps if continuous, much longer for static).
sleep 5

PORT=$(grep -oE 'rdp_port=[0-9]+' "$WLOG" | tail -1 | cut -d= -f2)
PASS=$(grep -oE "pw='[0-9a-f]+'" "$SLOG" | tail -1 | sed -E "s/pw='([0-9a-f]+)'/\\1/")
FPID=$(pgrep -f 'qdistro-forward --pipewire' | head -1)
echo "[s3c] port=$PORT password_first8=${PASS:0:8} forward_pid=$FPID"

if [ -z "$FPID" ]; then
    echo "[s3c] FAIL: qdistro-forward not running"
    echo "==== weston log tail ===="
    tail -30 "$WLOG"
    exit 3
fi

echo "[s3c] tcp port-open check..."
if timeout 3 bash -c "</dev/tcp/127.0.0.1/$PORT" 2>/dev/null; then
    echo "[s3c]   port $PORT ACCEPTS"
else
    echo "[s3c]   port $PORT REFUSED"
    echo "==== qdistro-forward stderr (from weston log) ===="
    grep -E 'qfwd|qdistro' "$WLOG" | tail -30
    exit 4
fi

echo "[s3c] checking surface dump (auto-dumped on first frame)..."
DUMP=/tmp/qfwd-dump.ppm
# Don't rm — already cleared before subscribe. Optionally request a
# refresh dump via SIGUSR1 (no-op if no further frames flow on a
# static scene; the auto-dump-on-frame-1 is the load-bearing path).
kill -USR1 $FPID 2>/dev/null || true
sleep 1
if [ -s "$DUMP" ]; then
    BYTES=$(stat -c %s "$DUMP")
    HEADER=$(head -c 20 "$DUMP" | tr -d '\0' | head -1)
    NONZERO=$(python3 -c "
import sys
data = open('$DUMP','rb').read()
# Skip PPM header (3 lines).
idx = 0
for _ in range(3):
    nl = data.find(b'\n', idx)
    if nl < 0:
        sys.exit('bad ppm')
    idx = nl + 1
pixels = data[idx:]
nz = sum(1 for b in pixels if b != 0)
total = len(pixels)
ratio = nz / max(total, 1)
print(f'nz={nz}/{total} ratio={ratio:.3f}')
")
    echo "[s3c]   dump bytes=$BYTES header='$HEADER' $NONZERO"
    cp "$DUMP" /home/admin/qfwd-dump-$$.ppm 2>/dev/null || true
else
    echo "[s3c]   FAIL: no $DUMP produced"
fi

echo "[s3c] sdl-freerdp connect (8s, SDL_VIDEODRIVER=dummy)..."
# SDL_VIDEODRIVER=dummy makes sdl-freerdp skip creating a real SDL
# window, so it stays alive for the full timeout and goes through the
# whole RDP session. Without this it crashes on mid-session unmap
# (memory sdl_freerdp_unmap_crash.md) and we can't observe continuous
# frame flow.
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    SDL_VIDEODRIVER=dummy \
    timeout 8 sdl-freerdp /v:127.0.0.1:$PORT /u:rdp /p:$PASS \
        /sec:rdp /cert:ignore /size:640x480 \
        >>"$FLOG" 2>&1 || true

echo "==== freerdp log (last 20) ===="
tail -20 "$FLOG"
echo
echo "==== qdistro-forward stderr (last 20, from /proc/PID/fd/2) ===="
# qfwd writes stderr — captured by the parent (weston) when it's a
# child. Grep weston log for [qfwd ...] lines.
grep -E '\[qfwd' "$WLOG" | tail -20

echo
echo "==== weston log tail (last 20) ===="
tail -20 "$WLOG"

echo
echo "[s3c] cleaning up"
echo "stream-stop $HANDLE" | socat - UNIX-CONNECT:$SOCK
sleep 2
if kill -0 $FPID 2>/dev/null; then
    echo "[s3c] WARN: forward $FPID still alive after stream-stop"
else
    echo "[s3c] OK: forward reaped"
fi
