#!/bin/bash
# §6.7 xdg-activation-v1 driver: boots weston with qdwin, runs the
# pywayland probe, checks compositor log + probe output.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdwin-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s7-weston.log
PLOG=/home/admin/s7-probe.log
INI=/home/admin/.config/weston.ini

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
pkill -9 -x weston 2>/dev/null || true
pkill -9 -f "qdshell.py" 2>/dev/null || true
pkill -9 -f "s7-xdg-activation-probe.py" 2>/dev/null || true
sleep 1

# Re-stage qdshell so the probe can resolve the installed pywayland
# xdg_activation_v1 bindings out of qdshell/protocol.
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

rm -f "$WLOG" "$PLOG"; touch "$WLOG" "$PLOG"; chown admin:admin "$WLOG" "$PLOG"

cat >/home/admin/run-s7-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s7-weston.sh; chown admin:admin /home/admin/run-s7-weston.sh

runuser -u admin -- nohup /home/admin/run-s7-weston.sh >>"$WLOG" 2>&1 </dev/null &

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done

chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

# Run the probe as admin (the compositor's allowed uid).
install -m 0644 /root/s7-xdg-activation-probe.py /home/admin/s7-xdg-activation-probe.py
chown admin:admin /home/admin/s7-xdg-activation-probe.py

set +e
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    QDSHELL_PROTO_DIR=/home/admin/qdshell \
    python3 /home/admin/s7-xdg-activation-probe.py 2>&1 | tee "$PLOG"
PROBE_RC=${PIPESTATUS[0]}
set -e

echo
echo "=== weston log (xdg-activation traces) ==="
grep "xdg-activation" "$WLOG" || echo "(none)"
echo

if [ "$PROBE_RC" -ne 0 ]; then
    echo "FAIL: probe exited $PROBE_RC"; exit "$PROBE_RC"
fi

grep -q "xdg-activation token issued" "$WLOG" || {
    echo "FAIL: compositor log missing 'token issued'"; exit 4
}
grep -q "xdg-activation activate with unknown token" "$WLOG" || {
    echo "FAIL: compositor log missing 'activate with unknown token'"; exit 5
}

echo "PASS: §6.7 xdg-activation-v1 end-to-end"
