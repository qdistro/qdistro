#!/bin/bash
# §6.8 cursor-sprite full theme — qdwin_shell_v1.set_cursor_sprite (v10).
#
# Smoke test for the protocol + cache path:
#   1. Bring up outer qdwin (RDP backend, like §6.5).
#   2. Spawn qdistro-cursor-sprites helper at uid=admin. It binds
#      qdwin_shell_v1 v10 and registers a synthetic wl_shm sprite for
#      shape POINTER (2). Stays paused on signalfd so the surface
#      survives.
#   3. Verify outer logs:
#        "qdwin: cursor-sprite registered shape=pointer hotspot=0,0"
#   4. Send SIGTERM to the helper. Verify outer logs:
#        "qdwin: cursor-sprite cleared shape=pointer"
#      (compositor's destroy listener fires when the wl_client's
#       wl_surface goes away.)
#
# Real XcursorImage upload via libXcursor is a self-contained iteration
# inside fill_sprite_buffer() in qdistro-cursor-sprites.c — out of
# scope for this smoke.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s29-weston-outer.log
HLOG=/home/admin/s29-helper.log
INI=/home/admin/.config/weston.ini

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
loginctl enable-linger admin 2>/dev/null || true

pkill -9 -x weston 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
pkill -9 -f qdistro-cursor-sprites 2>/dev/null || true
sleep 1

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
name=rdp-0
mode=1280x720
EOF
chown admin:admin "$INI"

rm -f "$WLOG" "$HLOG"; touch "$WLOG" "$HLOG"; chown admin:admin "$WLOG" "$HLOG"

# --- start outer qdwin ----------------------------------------------
cat >/home/admin/run-s29-outer.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --config=$INI \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s29-outer.sh; chown admin:admin /home/admin/run-s29-outer.sh
runuser -u admin -- nohup /home/admin/run-s29-outer.sh >>"$WLOG" 2>&1 </dev/null &
OUTPID=$!

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'qdwin: shell loaded' "$WLOG" || { echo "FAIL: outer not up"; exit 2; }
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true
echo "PASS: outer qdwin started"

# --- spawn cursor-sprites helper as admin ------------------------------
# XCURSOR_THEME=Adwaita: Tumbleweed ships Adwaita cursors but no
# /usr/share/icons/default symlink, so libXcursor's auto-fallback
# misses without an explicit theme name. Adwaita is in install-deps's
# adwaita-icon-theme package — guaranteed present on a bootstrapped
# VM. Forces the via=xcursor path through the new libXcursor lookup.
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    XCURSOR_THEME=Adwaita \
    QDWIN_CURSOR_SPRITES_SHAPES=4 \
    nohup /usr/bin/qdistro-cursor-sprites >>"$HLOG" 2>&1 </dev/null &
HPID=$!

for i in 1 2 3 4 5 6; do
    grep -q 'cursor-sprite registered shape=pointer' "$WLOG" 2>/dev/null && break
    sleep 1
done

if grep -q 'cursor-sprite registered shape=pointer' "$WLOG"; then
    echo "PASS: §6.8 cursor-sprite registered (POINTER) on outer"
else
    echo "FAIL: outer did not log cursor-sprite registered for POINTER"
    echo "--- helper log ---"; cat "$HLOG"
    echo "--- outer log tail ---"; tail -30 "$WLOG"
    exit 3
fi

if grep -q 'registration complete' "$HLOG"; then
    echo "PASS: helper completed registration cleanly"
else
    echo "WARN: helper did not log registration complete"
    cat "$HLOG"
fi

# Validate the libXcursor swap-in actually fired. With XCURSOR_THEME=
# Adwaita explicitly set above and adwaita-icon-theme present from
# install-deps, the via=xcursor path MUST hit. A via=synthetic here
# means either the theme didn't install (regression in install-deps)
# or libXcursor's name resolution broke — both warrant investigation,
# not a silent pass.
if grep -q 'registered shape=4 via=xcursor' "$HLOG"; then
    echo "PASS: helper used libXcursor theme for shape=pointer"
elif grep -q 'registered shape=4 via=synthetic' "$HLOG"; then
    echo "FAIL: helper fell back to synthetic despite XCURSOR_THEME=Adwaita"
    echo "--- helper log ---"; cat "$HLOG"
    echo "--- /usr/share/icons listing ---"
    ls /usr/share/icons/ 2>&1
    exit 6
else
    echo "FAIL: helper did not log any via= classification"
    cat "$HLOG"
    exit 6
fi

# --- helper teardown should clear the cache --------------------------
HPID=$(pgrep -f qdistro-cursor-sprites | head -1)
if [ -z "$HPID" ]; then
    echo "FAIL: helper not running for teardown phase"
    exit 4
fi
kill -TERM "$HPID"
for i in 1 2 3 4 5; do
    if ! kill -0 "$HPID" 2>/dev/null; then break; fi
    sleep 1
done

for i in 1 2 3 4 5; do
    grep -q 'cursor-sprite cleared shape=pointer' "$WLOG" 2>/dev/null && break
    sleep 1
done

if grep -q 'cursor-sprite cleared shape=pointer' "$WLOG"; then
    echo "PASS: §6.8 cursor-sprite cleared after helper exit"
else
    echo "FAIL: outer did not log cleared after helper exit"
    grep cursor-sprite "$WLOG" | tail -10
    exit 5
fi

# --- teardown -------------------------------------------------------
pkill -9 -f qdistro-cursor-sprites 2>/dev/null || true
kill "$OUTPID" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

echo
echo "PASS: §6.8 cursor-sprite v10 protocol + cache + destroy-listener end-to-end"
