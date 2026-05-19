#!/bin/bash
# §6.8 S0 — qdwin_nested_v1 stub bind + advertise smoke.
#
# Verifies the S0 stub:
#   1. The qdwin_nested_manager_v1 global shows up on the wl_registry.
#   2. A client bound as the allowed uid can call advertise_toplevel
#      and gets back a qdwin_nested_toplevel_v1 resource that receives
#      the `configured` event (at the S0 placeholder size 800x600).
#   3. The compositor log contains the "nested-toplevel advertise"
#      line showing pw_node / input_sink / app_id / title / origin_uid.
#
# Full nested→outer passthrough (S1 pipewire pub, S2 proxy surface,
# S3 input injection, S4 authz) is scoped to later §6.8 stages —
# see tasks/014-phase6.8-nested-passthrough.md.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s21-weston.log
PLOG=/home/admin/s21-probe.log
INI=/home/admin/.config/weston.ini

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
loginctl enable-linger admin 2>/dev/null || true

# Stop the admin user's production qdwin session first; otherwise
# Restart=on-failure relaunches weston between our pkill and our own
# weston's startup, racing the wayland-1 lockfile. Matches the s23/s25/
# s28 cleanup (R9) — needed when s21 runs as the first §6.8 probe in a
# full-file bats run and the qdwin user-session has just been spun up.
systemctl --machine=admin@.host --user stop \
    noctalia-shell.service noctalia-session.service qdlocker.service \
    2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true
pkill -9 -f 's21-nested-probe' 2>/dev/null || true
pkill -9 -f 'weston-terminal' 2>/dev/null || true
# Poll up to 10s until weston is gone *and* the lockfile (if present)
# is no longer flocked, then unlink stale on-disk artifacts.
for _ in $(seq 1 20); do
    pgrep -x weston >/dev/null 2>&1 && { sleep 0.5; continue; }
    if [ ! -e /run/user/1000/wayland-1.lock ] || \
       flock -n -x /run/user/1000/wayland-1.lock -c true 2>/dev/null; then
        break
    fi
    sleep 0.5
done
rm -f /run/user/1000/wayland-1.lock /run/user/1000/wayland-1 2>/dev/null || true
sleep 1

rm -rf /home/admin/qdshell
install -d -o admin -g admin /home/admin/qdshell
cp -r "$QDWIN_SRC/qdshell/." /home/admin/qdshell/
install -m 0644 "$QDWIN_SRC/qdwin/qdwin/qdwin-shell-v1.xml" \
    /home/admin/qdshell/qdwin-shell-v1.xml
install -m 0644 "$QDWIN_SRC/qdwin/qdwin/qdwin-nested-v1.xml" \
    /home/admin/qdshell/qdwin-nested-v1.xml
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

cat >/home/admin/run-s21-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s21-weston.sh; chown admin:admin /home/admin/run-s21-weston.sh
runuser -u admin -- nohup /home/admin/run-s21-weston.sh >>"$WLOG" 2>&1 </dev/null &
WESTONPID=$!

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'qdwin: shell loaded' "$WLOG" || {
    echo "FAIL: weston did not load qdwin-shell"
    tail -20 "$WLOG"
    exit 2
}
echo "PASS: weston + qdwin started"

chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

install -m 0644 /root/s21-nested-probe.py \
    /home/admin/s21-nested-probe.py
chown admin:admin /home/admin/s21-nested-probe.py

set +e
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    QDSHELL_PROTO_DIR=/home/admin/qdshell \
    python3 /home/admin/s21-nested-probe.py 2>&1 | tee "$PLOG"
PROBE_RC=${PIPESTATUS[0]}
set -e

if [ "$PROBE_RC" -ne 0 ]; then
    echo "FAIL: probe exited $PROBE_RC"
    tail -20 "$WLOG"
    exit "$PROBE_RC"
fi

echo
echo "=== nested traces in weston log ==="
grep -E 'nested_manager|nested-toplevel' "$WLOG" || echo "(none)"

grep -q 'nested_manager bound' "$WLOG" || {
    echo "FAIL: compositor never logged 'nested_manager bound'"
    exit 3
}
grep -q 'nested-toplevel advertise' "$WLOG" || {
    echo "FAIL: compositor never logged 'nested-toplevel advertise'"
    exit 4
}

pkill -9 -x weston 2>/dev/null || true
echo "PASS: §6.8 S0 qdwin_nested_v1 bind + advertise_toplevel"
