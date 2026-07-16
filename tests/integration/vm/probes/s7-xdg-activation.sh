#!/bin/bash
# §6.7 xdg-activation-v1 driver: boots a probe-private Weston with qdwin,
# runs the pywayland probe, and checks that compositor's own log.  It must not
# stop, reconfigure, or otherwise perturb the production wayland-1 session.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
WLOG=/home/admin/s7-weston.log
PLOG=/home/admin/s7-probe.log
INI=/home/admin/s7-weston.ini
PROTO_DIR=/home/admin/s7-qdshell
PIDFILE=/home/admin/s7-weston.pid
WL=wayland-s7
RUNTIME_DIR=/run/user/1000

cleanup() {
    local pid="" cmdline=""
    if [ -r "$PIDFILE" ]; then
        read -r pid <"$PIDFILE" || true
    fi
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
        cmdline=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
        case "$cmdline" in
            *weston*--socket=wayland-s7*)
                kill -TERM "$pid" 2>/dev/null || true
                for _ in 1 2 3 4 5; do
                    kill -0 "$pid" 2>/dev/null || break
                    sleep 0.2
                done
                kill -KILL "$pid" 2>/dev/null || true
                ;;
        esac
    fi
    rm -f "$PIDFILE" "$RUNTIME_DIR/$WL" "$RUNTIME_DIR/$WL.lock"
}
trap cleanup EXIT INT TERM

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
pkill -9 -f "s7-xdg-activation-probe.py" 2>/dev/null || true
cleanup

# Stage bindings in a probe-private directory. /home/admin/qdshell may back the
# production qdshell service and must not be replaced by an integration probe.
rm -rf "$PROTO_DIR"
install -d -o admin -g admin "$PROTO_DIR"
cp -r "$QDWIN_SRC/qdshell/." "$PROTO_DIR/"
install -m 0644 "$QDWIN_SRC/qdwin/qdwin/qdwin-shell-v1.xml" \
    "$PROTO_DIR/qdwin-shell-v1.xml"
chown -R admin:admin "$PROTO_DIR"
runuser -u admin -- env QDWIN_PROTO_XML="$PROTO_DIR/qdwin-shell-v1.xml" \
    "$PROTO_DIR/gen_protocol.sh" >/dev/null

# A headless output is sufficient for the protocol probe and avoids opening a
# second RDP listener beside the real desktop session.
cat >"$INI" <<EOF
[core]
shell=qdwin-shell.so
backend=headless-backend.so
require-outputs=any
idle-time=0

[shell]
locking=false

[output]
name=headless
mode=1280x720
EOF
chown admin:admin "$INI"

rm -f "$WLOG" "$PLOG"
touch "$WLOG" "$PLOG"
chown admin:admin "$WLOG" "$PLOG"

cat >/home/admin/run-s7-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=$RUNTIME_DIR
export QDWIN_ALLOWED_UID=1000
printf '%s\n' "\$\$" >$PIDFILE
exec weston \
    --socket=$WL \
    --config=$INI \
    --log=$WLOG
EOF
chmod +x /home/admin/run-s7-weston.sh
chown admin:admin /home/admin/run-s7-weston.sh

runuser -u admin -- nohup /home/admin/run-s7-weston.sh >>"$WLOG" 2>&1 </dev/null &

READY=0
for _ in $(seq 1 30); do
    if [ -S "$RUNTIME_DIR/$WL" ] && grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null; then
        READY=1
        break
    fi
    if [ -r "$PIDFILE" ]; then
        read -r weston_pid <"$PIDFILE" || true
        if [[ "$weston_pid" =~ ^[0-9]+$ ]] && ! kill -0 "$weston_pid" 2>/dev/null; then
            break
        fi
    fi
    sleep 0.5
done
if [ "$READY" != 1 ]; then
    cat "$WLOG" >&2 || true
    echo "FAIL: isolated s7 Weston did not become ready on $WL" >&2
    exit 2
fi

chmod 0600 "$RUNTIME_DIR/$WL" 2>/dev/null || true

# Run the probe as the compositor's allowed uid.
install -m 0644 /root/s7-xdg-activation-probe.py /home/admin/s7-xdg-activation-probe.py
chown admin:admin /home/admin/s7-xdg-activation-probe.py

set +e
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR="$RUNTIME_DIR" \
    WAYLAND_DISPLAY="$WL" \
    QDSHELL_PROTO_DIR="$PROTO_DIR" \
    python3 /home/admin/s7-xdg-activation-probe.py >"$PLOG" 2>&1
PROBE_RC=$?
set -e
cat "$PLOG"

# Verify only the isolated compositor log. A stale or production journal match
# must never let a broken probe compositor pass.
compositor_has() {
    local pat="$1"
    for _ in 1 2 3 4 5; do
        grep -q "$pat" "$WLOG" 2>/dev/null && return 0
        sleep 1
    done
    return 1
}

echo
echo "=== isolated compositor xdg-activation traces ($WLOG) ==="
traces=$(grep "xdg-activation" "$WLOG" 2>/dev/null || true)
[ -n "$traces" ] && echo "$traces" || echo "(none)"
echo

if [ "$PROBE_RC" -ne 0 ]; then
    echo "FAIL: probe exited $PROBE_RC"
    exit "$PROBE_RC"
fi

compositor_has "xdg-activation token issued" || {
    echo "FAIL: compositor log missing 'token issued'"
    exit 4
}
compositor_has "xdg-activation activate with unknown token" || {
    echo "FAIL: compositor log missing 'activate with unknown token'"
    exit 5
}

echo "PASS: §6.7 xdg-activation-v1 end-to-end"
