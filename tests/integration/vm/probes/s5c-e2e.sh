#!/bin/bash
# §6.5 S5c end-to-end smoke:
#   - bring up weston + qdshell with QDISTRO_FORWARD_NO_CLAIM=1 so
#     qdistro-forward spawns but does NOT claim the stream's input
#     handle; our pywayland probe claims instead.
#   - launch weston-terminal running a capture shell that appends every
#     read character to /tmp/s5c-typed.
#   - subscribe a stream, scrape the token from qdistro-forward argv.
#   - run s5c-inject-probe.py to claim + inject an ASCII word.
#   - verify /tmp/s5c-typed contains the injected word.
set -eo pipefail

WORD=${WORD:-hello}

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
WLOG=/home/admin/s5c-weston.log
SLOG=/home/admin/s5c-qdshell.log
PLOG=/home/admin/s5c-probe.log
SOCK=/tmp/qdshell-s5c.sock
INI=/home/admin/.config/weston.ini
CAPTURE=/tmp/s5c-typed
SHELL_LOG=/tmp/s5c-shell.log
CAPTURE_SH=/home/admin/s5c-capture-shell.sh
CERTDIR=/home/admin/qdwin-rdp

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
pkill -9 -x weston 2>/dev/null || true
pkill -9 weston-terminal 2>/dev/null || true
pkill -9 -f qdshell.py 2>/dev/null || true
pkill -9 -f qdistro-forward 2>/dev/null || true
sleep 1

rm -f "$WLOG" "$SLOG" "$PLOG" "$CAPTURE" "$SHELL_LOG"
touch "$WLOG" "$SLOG" "$PLOG"
chown admin:admin "$WLOG" "$SLOG" "$PLOG"

rm -rf /home/admin/qdshell
install -d -o admin -g admin /home/admin/qdshell
cp -r "$QDWIN_SRC/qdshell/." /home/admin/qdshell/
install -m 0644 "$QDWIN_SRC/qdwin/qdwin/qdwin-shell-v1.xml" \
    /home/admin/qdshell/qdwin-shell-v1.xml
chown -R admin:admin /home/admin/qdshell
runuser -u admin -- env QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    /home/admin/qdshell/gen_protocol.sh >/dev/null

# spike-6.5 probe: sync from host if present, else assume already there.
install -d -o admin -g admin /home/admin/spike-6.5
if [ -d "$QDWIN_SRC/spike-6.5" ]; then
    cp "$QDWIN_SRC/spike-6.5/s5c-inject-probe.py" /home/admin/spike-6.5/ 2>/dev/null || true
fi
chown -R admin:admin /home/admin/spike-6.5

# weston.ini — unchanged from s3c-e2e.
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

# Capture shell: stand-in for an interactive shell under weston-terminal.
# We read stdin char-by-char and append to $CAPTURE. bash's `read -N 1`
# gives a 1-byte read regardless of terminator. Prints nothing on
# stdout so the terminal stays a black box — ok, we verify via $CAPTURE.
cat >"$CAPTURE_SH" <<'EOF'
#!/bin/bash
exec >/tmp/s5c-shell.log 2>&1
echo "capture-shell started pid=$$"
while IFS= read -r -N 1 c; do
    printf '%s' "$c" >> /tmp/s5c-typed
done
echo "capture-shell read loop exited"
EOF
chmod +x "$CAPTURE_SH"
chown admin:admin "$CAPTURE_SH"
: > "$CAPTURE"; chown admin:admin "$CAPTURE"
: > "$SHELL_LOG"; chown admin:admin "$SHELL_LOG"

# Weston launcher — set QDISTRO_FORWARD_NO_CLAIM so the spawned
# qdistro-forward leaves the stream unclaimed for our probe, and
# QDWIN_STREAM_INPUT_DEBUG so inject_* breadcrumbs land in the log.
cat >/home/admin/run-s5c-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
export QDISTRO_FORWARD_NO_CLAIM=1
export QDWIN_STREAM_INPUT_DEBUG=1
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s5c-weston.sh
chown admin:admin /home/admin/run-s5c-weston.sh

runuser -u admin -- nohup /home/admin/run-s5c-weston.sh >>"$WLOG" 2>&1 </dev/null &
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
        --ctrl-socket=$SOCK >>"$SLOG" 2>&1 </dev/null &
for i in 1 2 3 4 5; do
    [ -S "$SOCK" ] && break
    sleep 1
done

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    nohup weston-terminal --shell="$CAPTURE_SH" >/dev/null 2>&1 </dev/null &
sleep 3

HANDLE=$(echo "list" | socat - UNIX-CONNECT:$SOCK | awk '/^tl /{print $2; exit}')
echo "[s5c] handle=$HANDLE"
[ -z "$HANDLE" ] && { echo "[s5c] FAIL: no toplevel"; exit 2; }

echo "stream $HANDLE s5c 640 480 0" | socat - UNIX-CONNECT:$SOCK
sleep 2

PORT=$(grep -oE 'rdp_port=[0-9]+' "$WLOG" | tail -1 | cut -d= -f2)
echo "[s5c] rdp_port=$PORT"

# Scrape token from qdistro-forward's argv. With NO_CLAIM set the
# forward is still running, still holds the token, but won't claim.
set +e
ARGS=$(ps -eo args --no-headers | grep -E 'qdistro-forward ' | grep -v grep | head -1)
set -e
TOKEN=$(echo "$ARGS" | sed -nE 's/.*--access-token ([0-9a-f]+).*/\1/p')
echo "[s5c] token_first8=${TOKEN:0:8} (len=${#TOKEN})"
if [ -z "$TOKEN" ]; then
    echo "[s5c] FAIL: no token"
    tail -20 "$WLOG"
    exit 3
fi

# Confirm qdistro-forward logged the skip.
grep -E 'NO_CLAIM|skipping claim' "$WLOG" | head -3 || true

echo "[s5c] running claim+inject probe (word='$WORD')..."
# pywayland's display-disconnect path segfaults after the probe finishes
# its real work; the captured bytes have already landed by then. Tolerate
# non-zero exit — the test's actual assertion is the content of $CAPTURE.
set +e
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    python3 /home/admin/spike-6.5/s5c-inject-probe.py wayland-1 "$TOKEN" "$WORD" \
    >"$PLOG" 2>&1
PROBE_EXIT=$?
set -e
echo "[s5c] probe exit=$PROBE_EXIT (segfault-on-disconnect is a known pywayland quirk)"
cat "$PLOG"

# Let the focused client drain wl_keyboard events.
sleep 1

echo
echo "==== qdwin inject/notify/seat trace ===="
grep -E 'stream seat|stream_input claim|notify_(motion|button|axis|key)' "$WLOG" | tail -40

echo
echo "==== capture shell log ===="
cat "$SHELL_LOG" 2>/dev/null || echo "(empty)"

echo
echo "==== captured bytes (/tmp/s5c-typed hex) ===="
xxd "$CAPTURE" 2>/dev/null || hexdump -C "$CAPTURE" 2>/dev/null
echo "==== captured string ===="
cat "$CAPTURE"
echo "[bytes=$(wc -c < "$CAPTURE")]"

echo
if grep -q "$WORD" "$CAPTURE" 2>/dev/null; then
    echo "[s5c] PASS: captured '$WORD' at focused surface"
    PASS=0
else
    echo "[s5c] FAIL: '$WORD' not found in capture"
    PASS=1
fi

echo
echo "[s5c] cleaning up"
echo "stream-stop $HANDLE" | socat - UNIX-CONNECT:$SOCK
sleep 1

exit $PASS
