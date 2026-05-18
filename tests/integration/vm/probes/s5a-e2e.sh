#!/bin/bash
# §6.5 S5a e2e: subscribe a stream, read the access_token from qdwin's
# log, run the python probe to bind qdwin_stream_input_v1 and call
# claim(token). Verify qdwin logs `claim OK` and inject stubs fire.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
WLOG=/home/admin/s5a-weston.log
SLOG=/home/admin/s5a-qdshell.log
SOCK=/tmp/qdshell-s5a.sock
INI=/home/admin/.config/weston.ini
CERTDIR=/home/admin/qdwin-rdp

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
pkill -9 -x weston 2>/dev/null || true
pkill -9 weston-terminal 2>/dev/null || true
pkill -9 -f qdshell.py 2>/dev/null || true
pkill -9 -f qdistro-forward 2>/dev/null || true
pkill -9 -f s5a-claim-probe 2>/dev/null || true
sleep 1

rm -rf /home/admin/qdshell
install -d -o admin -g admin /home/admin/qdshell
cp -r "$QDWIN_SRC/qdshell/." /home/admin/qdshell/
install -m 0644 "$QDWIN_SRC/qdwin/qdwin-shell-v1.xml" \
    /home/admin/qdshell/qdwin-shell-v1.xml
chown -R admin:admin /home/admin/qdshell
runuser -u admin -- env QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    /home/admin/qdshell/gen_protocol.sh >/dev/null

# Copy the probe into admin's home so its sys.path-fix finds the
# generated protocol bindings under /home/admin/qdshell/protocol/.
install -d -o admin -g admin /home/admin/spike-6.5
# Fetch fresh from the host to pick up edits without re-running
# s3c-sync-and-build.sh's source sync.
wget -q http://10.0.2.2:8765/spike-6.5/s5a-claim-probe.py \
    -O /home/admin/spike-6.5/s5a-claim-probe.py
chown -R admin:admin /home/admin/spike-6.5
chmod +x /home/admin/spike-6.5/s5a-claim-probe.py

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

rm -f "$WLOG" "$SLOG"; touch "$WLOG" "$SLOG"
chown admin:admin "$WLOG" "$SLOG"

cat >/home/admin/run-s5a-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
# Since S5b, qdistro-forward auto-claims the stream's input handle on
# startup; the probe would collide with already_claimed. NO_CLAIM tells
# qdistro-forward to leave the token free so the probe can claim.
export QDISTRO_FORWARD_NO_CLAIM=1
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s5a-weston.sh
chown admin:admin /home/admin/run-s5a-weston.sh

runuser -u admin -- nohup /home/admin/run-s5a-weston.sh >>"$WLOG" 2>&1 </dev/null &
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
for i in 1 2 3 4 5; do [ -S "$SOCK" ] && break; sleep 1; done

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    nohup weston-terminal >/dev/null 2>&1 </dev/null &
sleep 3

HANDLE=$(echo "list" | socat - UNIX-CONNECT:$SOCK | awk '/^tl /{print $2; exit}')
echo "[s5a] handle=$HANDLE"
[ -z "$HANDLE" ] && { echo "[s5a] FAIL: no toplevel"; exit 2; }

echo "stream $HANDLE diag 640 480 0" | socat - UNIX-CONNECT:$SOCK
sleep 2

# Token is passed only via spawn argv to qdistro-forward (not in any
# wayland event). Scrape it from the running process's command line.
set +e
ARGS=$(ps -eo args --no-headers | grep qdistro-forward | grep -v grep | head -1)
set -e
TOKEN=$(echo "$ARGS" | sed -nE 's/.*--access-token ([0-9a-f]+).*/\1/p')
echo "[s5a] token_first8=${TOKEN:0:8} (len=${#TOKEN})"
if [ -z "$TOKEN" ]; then
    echo "[s5a] FAIL: token not extracted from: $ARGS"
    exit 3
fi

PORT=$(grep -oE 'rdp_port=[0-9]+' "$WLOG" | tail -1 | cut -d= -f2)
echo "[s5a] rdp_port=$PORT"

# qdistro-forward is running with NO_CLAIM set (see run-s5a-weston.sh
# above), so the token is still free for the probe to claim.
# pywayland's disconnect path may segfault at shutdown (benign; the
# claim has already happened by then). Tolerate non-zero exit.
echo "[s5a] running claim probe..."
set +e
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    python3 /home/admin/spike-6.5/s5a-claim-probe.py wayland-1 "$TOKEN" \
    2>&1
PROBE_EXIT=$?
set -e
echo "[s5a] probe exit=$PROBE_EXIT"

echo
echo "==== weston log: stream_input + inject ===="
grep -E 'stream_input|inject ' "$WLOG" | tail -20

# Primary assertion: qdwin logged the successful claim.
if grep -q 'stream_input claim OK' "$WLOG"; then
    echo "[s5a] PASS: claim accepted"
    PASS=0
else
    echo "[s5a] FAIL: no 'claim OK' in weston log"
    PASS=1
fi

echo
echo "[s5a] cleaning up"
echo "stream-stop $HANDLE" | socat - UNIX-CONNECT:$SOCK
sleep 1
exit $PASS
