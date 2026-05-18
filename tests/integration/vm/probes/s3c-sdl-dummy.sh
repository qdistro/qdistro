#!/bin/bash
# Spike: can sdl-freerdp run headless with SDL_VIDEODRIVER=dummy?
# If yes, it becomes the stable headless RDP client for our smokes,
# replacing the default sdl-freerdp which crashes on mid-session unmap
# (see memory sdl_freerdp_unmap_crash.md).
#
# Success criterion: sdl-freerdp stays alive for >5s, gets past TLS
# handshake into an active session (server sees >1 frame request).
set -eo pipefail

WLOG=/home/admin/sdl-probe-weston.log
SLOG=/home/admin/sdl-probe-qdshell.log
FLOG=/home/admin/sdl-probe-freerdp.log
SOCK=/tmp/qdshell-sdl-probe.sock
INI=/home/admin/.config/weston.ini
CERTDIR=/home/admin/qdwin-rdp
QDWIN_SRC=/root/qdistro-src

pgrep -x pipewire >/dev/null
pkill -9 -x weston 2>/dev/null || true
pkill -9 weston-terminal 2>/dev/null || true
pkill -9 -f qdshell.py 2>/dev/null || true
pkill -9 -f qdistro-forward 2>/dev/null || true
pkill -9 -f sdl-freerdp 2>/dev/null || true
sleep 1
rm -f "$WLOG" "$SLOG" "$FLOG"
touch "$WLOG" "$SLOG" "$FLOG"; chown admin:admin "$WLOG" "$SLOG" "$FLOG"

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

cat >/home/admin/sdl-probe-run.sh <<WEND
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston --rdp-tls-cert=$CERTDIR/rdp.crt --rdp-tls-key=$CERTDIR/rdp.key --log=$WLOG
WEND
chmod +x /home/admin/sdl-probe-run.sh
chown admin:admin /home/admin/sdl-probe-run.sh
runuser -u admin -- nohup /home/admin/sdl-probe-run.sh >>"$WLOG" 2>&1 </dev/null &
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
echo "[sdl-probe] handle=$HANDLE"
echo "stream $HANDLE sdl-probe 640 480 0" | socat - UNIX-CONNECT:$SOCK
sleep 2

PORT=$(grep -oE 'rdp_port=[0-9]+' "$WLOG" | tail -1 | cut -d= -f2)
PASS=$(grep -oE "pw='[0-9a-f]+'" "$SLOG" | tail -1 | sed -E "s/pw='([0-9a-f]+)'/\\1/")
FPID=$(pgrep -f 'qdistro-forward --pipewire' | head -1)
echo "[sdl-probe] port=$PORT password_first8=${PASS:0:8} forward_pid=$FPID"

echo "[sdl-probe] launching sdl-freerdp with SDL_VIDEODRIVER=dummy, 10s timeout"
START=$(date +%s)
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    SDL_VIDEODRIVER=dummy \
    timeout 10 sdl-freerdp /v:127.0.0.1:$PORT /u:rdp /p:$PASS \
        /sec:rdp /cert:ignore /size:640x480 \
        >>"$FLOG" 2>&1 &
FRPID=$!
wait $FRPID 2>/dev/null || true
END=$(date +%s)
ELAPSED=$((END - START))
echo "[sdl-probe] sdl-freerdp ran $ELAPSED seconds"

echo
echo "==== freerdp log tail ===="
tail -30 "$FLOG"
echo
echo "==== qfwd log (frame counts) ===="
grep -E 'on_stream_process|shadow_client' "$WLOG" | tail -20
echo
echo "==== verdict ===="
FRAMES=$(grep -cE 'on_stream_process tick' "$WLOG" || true)
echo "[sdl-probe] on_stream_process ticks logged: $FRAMES"
if [ "$ELAPSED" -ge 8 ] && [ "$FRAMES" -ge 3 ]; then
    echo "[sdl-probe] PASS: stayed alive 10s, $FRAMES frame-mark log lines (~$((FRAMES*60)) frames)"
    VERDICT=0
else
    echo "[sdl-probe] FAIL: elapsed=${ELAPSED}s frames_logged=${FRAMES}"
    VERDICT=1
fi

echo "stream-stop $HANDLE" | socat - UNIX-CONNECT:$SOCK
sleep 1
exit $VERDICT
