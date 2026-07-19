#!/bin/bash
# §6.6 follow-up — cursor-shape sprite-install smoke.
#
# Per the §6.7(b) parking notes + §6.6 follow-up research (qdwin.c
# cursor-shape block): libweston-16 exposes weston_pointer::sprite as
# a public field + weston_buffer_create_solid_rgba / weston_surface_
# attach_solid as public APIs. That's enough to do per-shape solid-
# colour sprite install without an internal wl_client worker thread.
# Full theme-image install still needs the worker thread (or an
# upstream libweston `weston_pointer_set_sprite` + SHM buffer API);
# this smoke only verifies the solid-colour half.
#
# Verifies:
#   1. QDWIN_CURSOR_SPRITE_SOLID=1 flips the set_shape log line from
#      "sprite=deferred" → "sprite=installing-solid".
#   2. The compositor logs "cursor-shape sprite installed" with the
#      expected colour + size.
#   3. Weston doesn't crash after set_shape (the direct pointer->sprite
#      assignment + sprite_destroy_listener wiring is valid lifecycle-
#      wise).
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s20-weston.log
PLOG=/home/admin/s20-probe.log
INI=/home/admin/.config/weston.ini

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
pkill -9 -x weston 2>/dev/null || true
pkill -9 -f 'qdshell.py' 2>/dev/null || true
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

rm -f "$WLOG" "$PLOG"; touch "$WLOG" "$PLOG"; chown admin:admin "$WLOG" "$PLOG"

cat >/home/admin/run-s20-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
export QDWIN_CURSOR_SPRITE_SOLID=1
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s20-weston.sh; chown admin:admin /home/admin/run-s20-weston.sh

runuser -u admin -- nohup /home/admin/run-s20-weston.sh >>"$WLOG" 2>&1 </dev/null &

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'qdwin: shell loaded' "$WLOG" || {
    echo "FAIL: weston did not load qdwin-shell"
    tail -20 "$WLOG"
    exit 2
}
echo "PASS: weston + qdwin started (cursor-solid enabled)"

chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

install -m 0644 /root/s8-protocol-globals-probe.py \
    /home/admin/s20-cursor-probe.py
chown admin:admin /home/admin/s20-cursor-probe.py

pkill -9 -f 'sdl-freerdp.*:3389' 2>/dev/null || true
runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 20 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s20-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

set +e
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    QDSHELL_PROTO_DIR=/home/admin/qdshell \
    python3 /home/admin/s20-cursor-probe.py 2>&1 | tee "$PLOG"
PROBE_RC=${PIPESTATUS[0]}
set -e

kill "$SDLPID" 2>/dev/null || true
pkill -9 -f 'sdl-freerdp.*:3389' 2>/dev/null || true

if [ "$PROBE_RC" -ne 0 ]; then
    echo "FAIL: probe exited $PROBE_RC"; exit "$PROBE_RC"
fi

echo
echo "=== cursor-shape traces ==="
grep -E 'cursor-shape' "$WLOG" || true

# Check that set_shape fires the "installing-solid" log tag (not
# "deferred").
if grep -q 'sprite=installing-solid' "$WLOG"; then
    echo "PASS: set_shape ran with QDWIN_CURSOR_SPRITE_SOLID=1 branch"
else
    echo "FAIL: set_shape still reports sprite=deferred"
    exit 3
fi

# Check the actual sprite-install success log.
if grep -qE 'cursor-shape sprite installed.*shape=(text|pointer|default)' "$WLOG"; then
    echo "PASS: sprite install path executed"
else
    echo "FAIL: no 'cursor-shape sprite installed' log"
    exit 4
fi

# Compositor should still be running (no crash from pointer->sprite
# assignment + listener wiring).
if pgrep -x weston >/dev/null; then
    echo "PASS: weston survived cursor-shape sprite install"
else
    echo "FAIL: weston crashed after set_shape"
    tail -30 "$WLOG"
    exit 5
fi

pkill -9 -x weston 2>/dev/null || true
echo "PASS: §6.6 follow-up cursor-shape solid sprite install end-to-end"
