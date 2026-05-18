#!/bin/bash
# §6.5 S4 broker-gate probe. Exercises qdshell's CheckPermission flow
# under broker-absent conditions. Two modes via the $MODE env var:
#
#   MODE=required_fail   QDSHELL_BROKER_REQUIRED=1, expect stream command
#                        to return an error referencing "broker required".
#   MODE=optional_allow  QDSHELL_BROKER_REQUIRED unset (default 0), expect
#                        the warning "broker unavailable" in the qdshell log
#                        AND a successful subscribe that spawns qdistro-
#                        forward as usual.
#
# This probe is intentionally light on scaffolding — it reuses the
# same pipewire+weston+weston-terminal setup as s3c-e2e.sh. The
# decisive checks are on the *qdshell ctrl response* and the
# *qdshell log line*, not on any compositor-side state change, so
# the probe can live beside the existing E2E tests without a separate
# VM.
set -eo pipefail

MODE=${MODE:-optional_allow}
QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s4-weston.log
SLOG=/home/admin/s4-qdshell.log
SOCK=/tmp/qdshell-s4.sock
INI=/home/admin/.config/weston.ini

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
pkill -9 -x weston 2>/dev/null || true
pkill -9 weston-terminal 2>/dev/null || true
pkill -9 -f qdshell.py 2>/dev/null || true
pkill -9 -f qdistro-forward 2>/dev/null || true
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

rm -f "$WLOG" "$SLOG"
touch "$WLOG" "$SLOG"
chown admin:admin "$WLOG" "$SLOG"

cat >/home/admin/run-s4-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s4-weston.sh
chown admin:admin /home/admin/run-s4-weston.sh

runuser -u admin -- nohup /home/admin/run-s4-weston.sh >>"$WLOG" 2>&1 </dev/null &
for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

if [ "$MODE" = "required_fail" ]; then
    # Default since 2026-04-23 (commit forthcoming) is already
    # required=1; pinning it here so the scenario stays correct even
    # if the default changes back.
    QDSHELL_REQ="QDSHELL_BROKER_REQUIRED=1"
else
    # optional_allow explicitly opts out of fail-closed. Before the
    # default flip, unset was equivalent; after the flip we must set
    # =0 to reach the auto-approve path.
    QDSHELL_REQ="QDSHELL_BROKER_REQUIRED=0"
fi

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    $QDSHELL_REQ \
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
[ -z "$HANDLE" ] && { echo "FAIL: no toplevel"; exit 2; }
echo "handle=$HANDLE mode=$MODE"

REPLY=$(echo "stream $HANDLE testlabel 640 480 0" | \
    socat - UNIX-CONNECT:$SOCK)
echo "ctrl reply: $REPLY"

if [ "$MODE" = "required_fail" ]; then
    # Expect error + broker-required mention in the reply.
    case "$REPLY" in
        err*stream*broker*required*unavailable*)
            echo "PASS: required mode fails closed"
            exit 0
            ;;
        *)
            echo "FAIL: expected 'broker required but unavailable' error"
            echo "--- qdshell log ---"
            tail -30 "$SLOG"
            exit 3
            ;;
    esac
fi

# optional_allow: broker-absent warning must appear, and the stream
# must succeed (auto-approve fallback).
case "$REPLY" in
    ok*stream*awaiting)
        if grep -qE 'broker unavailable.*auto-approving' "$SLOG"; then
            echo "PASS: broker-absent auto-approve with warning log"
            exit 0
        else
            echo "FAIL: missing expected 'broker unavailable' log line"
            tail -30 "$SLOG"
            exit 4
        fi
        ;;
    *)
        echo "FAIL: expected 'ok stream ... awaiting' reply"
        tail -30 "$SLOG"
        exit 5
        ;;
esac
