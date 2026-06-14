#!/bin/bash
# §6.8 S3e — a PENDING (broker-unapproved) nested proxy must NOT be input-
# pickable, even after a pixel feed binds (F6#2 mechanism A).
#
# This guards the broker boundary against the pixel-bind-while-pending path:
# a v9 shell receives nested_proxy_pixel_source at proxy-create (before the
# admin `allow`), so qdistro-nested-pixelfeed can call bind_proxy_pixels while
# the proxy is still held. A client pixel surface has a default-FULL input
# region, so if it became the active view it would let weston_compositor_
# pick_view route pointer input to an UNAPPROVED proxy. Mechanism A defers the
# curtain->pixel swap until allow: while pending, bind_proxy_pixels STASHES the
# surface and keeps the empty-input curtain as the active view.
#
# This probe forces the pending state with QDWIN_NESTED_BROKER_REQUIRED=1 (so
# the proxy is held even without a v9 qdshell), drives bind_proxy_pixels by
# running qdistro-nested-pixelfeed directly (NO_PW solid-colour, no pipewire),
# and asserts:
#   - outer logs "pixel feed STASHED — proxy pending" (swap deferred, NOT
#     "curtain swapped");
#   - the deferred S3d route-test (env QDWIN_NESTED_S3D_TEST=1) fires the
#     pending-check: pick_view at the proxy centre does NOT return the proxy
#     (pending_unpickable=1) — the active view is the empty-input curtain.
#
# NOTE: the post-`allow` half (the same proxy becomes pickable + routes) needs
# the v9 qdshell + broker harness that is still skipped (Phase-3 rewrite); it is
# tracked in open-followups.md. s3d already proves the visible-proxy routing.
#
# Exit codes: 0 PASS; 2 outer; 3 nested; 5 no proxy; 6 phantom;
#             7 bind not deferred (swap happened while pending!); 8 pending
#             proxy was pickable.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s3e-weston-outer.log
NLOG=/home/admin/s3e-weston-nested.log
PFLOG=/home/admin/s3e-pixelfeed.log
INI=/home/admin/.config/weston.ini
NESTED_INI=/home/admin/.config/weston-nested-pub.ini

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
command -v qdistro-nested-pixelfeed >/dev/null 2>&1 \
    || { echo "SKIP: qdistro-nested-pixelfeed not installed"; exit 0; }
loginctl enable-linger admin 2>/dev/null || true

systemctl --machine=admin@.host --user stop \
    noctalia-shell.service noctalia-session.service qdlocker.service \
    2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
pkill -9 -f "weston-terminal" 2>/dev/null || true
pkill -9 -f qdistro-nested-pixelfeed 2>/dev/null || true
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

# --- stage qdshell protocol XMLs (the pixelfeed needs qdwin-shell-v1) ---
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
backend=rdp-backend.so,pipewire-backend.so
require-outputs=any
idle-time=0

[shell]
locking=false

[output]
name=rdp-0
mode=1280x720

[pipewire]
num-outputs=1
EOF
chown admin:admin "$INI"

# nested publisher: PIPEWIRE-ONLY (the F6#2 config), same as the s3d lane.
cat >"$NESTED_INI" <<EOF
[core]
shell=qdwin-shell.so
backend=pipewire-backend.so
require-outputs=any
idle-time=0

[shell]
locking=false

[pipewire]
num-outputs=8
EOF
chown admin:admin "$NESTED_INI"

rm -f "$WLOG" "$NLOG" "$PFLOG"; touch "$WLOG" "$NLOG" "$PFLOG"
chown admin:admin "$WLOG" "$NLOG" "$PFLOG"

# --- outer qdwin: broker-required (forces pending) + S3d route-test ----
cat >/home/admin/run-s3e-outer.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
export QDWIN_NESTED_S3D_TEST=1
export QDWIN_NESTED_BROKER_REQUIRED=1
exec weston \\
    --config=$INI \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s3e-outer.sh; chown admin:admin /home/admin/run-s3e-outer.sh
runuser -u admin -- nohup /home/admin/run-s3e-outer.sh >>"$WLOG" 2>&1 </dev/null &
OUTPID=$!

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'qdwin: shell loaded' "$WLOG" || {
    echo "FAIL: outer weston did not load qdwin-shell"; tail -20 "$WLOG"; exit 2; }
echo "PASS: outer qdwin started (broker-required + S3d route-test)"
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 90 sdl-freerdp /v:127.0.0.1:3389 /cert:ignore /u:probe /p:probe \
        >/tmp/s3e-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

# --- nested publisher (pipewire-only) ----------------------------------
cat >/home/admin/run-s3e-nested.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-1
export QDWIN_NESTED_MODE=1
export QDWIN_OUTER_DISPLAY=wayland-1
export QDWIN_ALLOWED_UID=1000
exec weston --config=$NESTED_INI -Sqdwin-nested-pub --log=$NLOG
EOF
chmod +x /home/admin/run-s3e-nested.sh; chown admin:admin /home/admin/run-s3e-nested.sh
runuser -u admin -- nohup /home/admin/run-s3e-nested.sh >>"$NLOG" 2>&1 </dev/null &
NESTEDPID=$!

for i in $(seq 1 15); do
    sleep 1
    grep -q 'nested-mode publisher ready' "$NLOG" 2>/dev/null && break
done
grep -q 'nested-mode publisher ready' "$NLOG" || {
    echo "FAIL: pipewire-only nested weston did not init publisher mode"
    tail -40 "$NLOG"; pkill -9 -x weston 2>/dev/null || true; exit 3; }
echo "PASS: pipewire-only nested weston publisher mode ready"

# inner client -> advertise -> PENDING proxy on the outer
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=qdwin-nested-pub \
    nohup weston-terminal >/tmp/s3e-wt.log 2>&1 </dev/null &
WTPID=$!

for i in $(seq 1 15); do
    sleep 1
    grep -q 'qdwin/nested-proxy: created handle=' "$WLOG" 2>/dev/null && break
done
PROXY_LINE=$(grep 'qdwin/nested-proxy: created handle=' "$WLOG" | head -1)
if [ -z "$PROXY_LINE" ]; then
    echo "FAIL: outer never created the nested proxy"; tail -30 "$WLOG"
    kill "$WTPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true; exit 5
fi
# Confirm it is PENDING (broker-required) — pending=1 in the created line.
echo "proxy line: $PROXY_LINE"
if ! echo "$PROXY_LINE" | grep -q "pending=1"; then
    echo "FAIL: proxy is not pending despite QDWIN_NESTED_BROKER_REQUIRED=1"
    kill "$WTPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true; exit 5
fi
echo "PASS: outer created a PENDING nested proxy (broker-required)"
HANDLE=$(echo "$PROXY_LINE" | sed -n 's/.*created handle=\([0-9]*\).*/\1/p')

# no-phantom (pipewire-only nested), same invariant as s3d
NPID=$(grep -oE 'NESTED_MODE on; pid=[0-9]+' "$NLOG" | head -1 | grep -oE '[0-9]+' || true)
if [ -n "$NPID" ] && \
   grep -E "qdwin: toplevel_added handle=[0-9]+ uid=[0-9]+ pid=$NPID " "$WLOG"; then
    echo "FAIL: phantom regression — regular toplevel from nested pid=$NPID"
    kill "$WTPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true; exit 6
fi
echo "PASS: no phantom host-output toplevel (pid=${NPID:-?})"

# pw_node for the pixelfeed argv (NO_PW ignores pipewire, but argv requires it)
ADV_LINE=$(grep 'nested-toplevel advertise' "$WLOG" | head -1)
PWNODE=$(echo "$ADV_LINE" | sed -n "s/.*pw_node='\([^']*\)'.*/\1/p")
[ -z "$PWNODE" ] && PWNODE="weston.pipewire:0:none"

# --- drive bind_proxy_pixels WHILE PENDING via the pixelfeed directly ---
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 QDWIN_PIXELFEED_NO_PW=1 QDWIN_PIXELFEED_HOLD=12 \
    QDWIN_PIXELFEED_RGBA=0xff3030ff \
    nohup qdistro-nested-pixelfeed "$HANDLE" "$PWNODE" \
        >"$PFLOG" 2>&1 </dev/null &
PFPID=$!

# Assert the swap was DEFERRED (mechanism A), NOT performed while pending.
for i in $(seq 1 12); do
    grep -q "bind_proxy_pixels handle=$HANDLE" "$WLOG" 2>/dev/null && break
    sleep 1
done
if grep -qE "bind_proxy_pixels handle=$HANDLE surface=.* \(curtain swapped for live feed\)" "$WLOG"; then
    echo "FAIL: bind_proxy_pixels SWAPPED the curtain while pending — a pending"
    echo "      proxy's pixel surface became the active (pickable) view (exit 7)"
    grep "bind_proxy_pixels handle=$HANDLE" "$WLOG"
    kill "$WTPID" "$PFPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true; exit 7
fi
if grep -qE "bind_proxy_pixels handle=$HANDLE surface=.* \(pixel feed STASHED" "$WLOG"; then
    echo "PASS: bind_proxy_pixels while pending was DEFERRED (pixel feed stashed)"
else
    echo "FAIL: did not observe the deferred-stash log for handle=$HANDLE"
    echo "--- pixelfeed log ---"; tail -20 "$PFLOG"
    echo "--- outer tail ---"; tail -20 "$WLOG"
    kill "$WTPID" "$PFPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true; exit 7
fi

# Assert the deferred S3d pending-check found the proxy UNPICKABLE. The active
# view is the empty-input curtain (unchanged by the deferred bind), so this
# holds whether the check fired before or after the bind.
for i in $(seq 1 10); do
    grep -q "S3d pending-check handle=$HANDLE" "$WLOG" 2>/dev/null && break
    sleep 1
done
PCHK=$(grep "S3d pending-check handle=$HANDLE" "$WLOG" | head -1)
echo "pending-check: ${PCHK:-<none>}"
if echo "$PCHK" | grep -q "pending_unpickable=1"; then
    echo "PASS: pending nested proxy is NOT input-pickable (pick_view skips it)"
else
    echo "FAIL: pending proxy was pickable (or check did not fire) — exit 8"
    kill "$WTPID" "$PFPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true; exit 8
fi

# --- teardown ---
kill "$WTPID" "$PFPID" 2>/dev/null || true
pkill -9 -f weston-terminal 2>/dev/null || true
pkill -9 -f qdistro-nested-pixelfeed 2>/dev/null || true
kill "$NESTEDPID" 2>/dev/null || true
pkill -9 -f run-s3e-nested 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
kill "$OUTPID" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

echo
echo "PASS: §6.8 S3e pending nested proxy stays input-transparent through bind"
