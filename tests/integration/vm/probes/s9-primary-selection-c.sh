#!/bin/bash
# §6.7 primary-selection-v1 end-to-end driver (C probe variant).
#
# Drop-in replacement for s9-primary-selection.sh that runs the C
# libwayland-client probe instead of the pywayland one. The Python
# version can't decode the data_offer new_id event (pywayland 0.4.x
# limitation). See memory/pywayland_newid_event_limit.md.
#
# Builds the probe in-VM from s9-primary-selection.c + the wayland-
# protocols XML at /usr/share/wayland-protocols/unstable/primary-
# selection/primary-selection-unstable-v1.xml, then launches weston
# + qdwin-shell + an RDP peer (needed for seat creation), and runs
# the probe.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
# s9-primary-selection.c is staged to /root/ by fresh-vm-bootstrap.sh,
# not into the qdwin-src tree — use that copy. Fall back to the
# qdwin-src/spike-6.5 location if a newer sync has populated it.
SOURCE_C=/root/s9-primary-selection.c
[ -f "$QDWIN_SRC/spike-6.5/s9-primary-selection.c" ] && \
    SOURCE_C="$QDWIN_SRC/spike-6.5/s9-primary-selection.c"
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s9c-weston.log
PLOG=/home/admin/s9c-probe.log
INI=/home/admin/.config/weston.ini
PROTO_XML=/usr/share/wayland-protocols/unstable/primary-selection/primary-selection-unstable-v1.xml
BUILDDIR=/home/admin/s9c-build
PROBE=$BUILDDIR/s9-primary-selection

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
[ -f "$PROTO_XML" ] || { echo "ERROR: missing $PROTO_XML"; exit 1; }

pkill -9 -x weston 2>/dev/null || true
# Match the built probe binary (not the .sh/.c source files which
# include `s9-primary-selection` and would otherwise kill us).
pkill -9 -x s9-primary-selection 2>/dev/null || true
sleep 1

install -d -o admin -g admin "$BUILDDIR"
wayland-scanner client-header "$PROTO_XML" \
    "$BUILDDIR/primary-selection-unstable-v1-client-protocol.h"
wayland-scanner private-code   "$PROTO_XML" \
    "$BUILDDIR/primary-selection-unstable-v1.c"
install -m 0644 "$SOURCE_C" \
    "$BUILDDIR/s9-primary-selection.c"
chown -R admin:admin "$BUILDDIR"

cc -O1 -Wall -I"$BUILDDIR" $(pkg-config --cflags wayland-client) \
    "$BUILDDIR/s9-primary-selection.c" \
    "$BUILDDIR/primary-selection-unstable-v1.c" \
    -o "$PROBE" $(pkg-config --libs wayland-client)
chown admin:admin "$PROBE"

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

cat >/home/admin/run-s9c-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s9c-weston.sh; chown admin:admin /home/admin/run-s9c-weston.sh
runuser -u admin -- nohup /home/admin/run-s9c-weston.sh >>"$WLOG" 2>&1 </dev/null &

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

# rdp-backend only creates a wl_seat when a peer connects.
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 30 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s9c-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

set +e
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    "$PROBE" 2>&1 | tee "$PLOG"
PROBE_RC=${PIPESTATUS[0]}
set -e

kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true

if [ "$PROBE_RC" -ne 0 ]; then
    echo "FAIL: probe exited $PROBE_RC"; exit "$PROBE_RC"
fi
grep -q "B: PASS" "$PLOG" || {
    echo "FAIL: probe log missing B: PASS"; exit 4
}
echo "PASS: §6.7 primary-selection end-to-end (C)"
