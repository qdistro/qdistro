#!/bin/bash
# §6.8 S3d — nested-proxy pointer-INPUT ROUTING + no-phantom regression
# (F6#2). Builds on s23/s25 but proves the two halves of the F6#2 fix:
#
#   1. The CONFIG fix: the nested (publisher) weston runs a PIPEWIRE-ONLY
#      backend (backend=pipewire-backend.so) — NO wayland-backend. The
#      wayland-backend used to also create a host-output WINDOW (a regular
#      app_id=null xdg_toplevel) on the outer that shadowed the nested
#      proxy in the outer's weston_compositor_pick_view, leaving
#      active_input_proxy NULL. With it gone, NO regular toplevel_added
#      line on the outer carries the nested publisher's pid → "no phantom".
#
#   2. The ROUTING oracle: QDWIN_NESTED_S3D_TEST=1 on the OUTER makes the
#      advertise handler drive the REAL pointer routing path (set the seat
#      pointer focus to the proxy view == what pick_view now resolves to,
#      run qdwin_proxy_pointer_track_focus, then notify_button). Unlike the
#      S3b/S3c synthetic bursts it does NOT set active_input_proxy directly,
#      so it asserts the chain pick→track-focus→active_input_proxy→QDNI
#      button-forward actually fires. Outer logs
#      "S3d route-test ... active_input_proxy_matched=1"; the inner weston
#      decodes "qdwin/nested: button handle=N btn=0x110 state=1/0".
#
# Exit codes:
#   0 — PASS
#   2 — outer qdwin failed to start
#   3 — nested weston (pipewire-only) failed to start publisher mode
#   4 — publisher never bound the manager on the outer
#   5 — no advertise_toplevel / proxy on the outer
#   6 — PHANTOM regression: a regular toplevel from the nested pid appeared
#   7 — S3d route-test did not resolve active_input_proxy to the proxy
#   8 — inner weston did not decode the routed QDNI button
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s3d-weston-outer.log
NLOG=/home/admin/s3d-weston-nested.log
INI=/home/admin/.config/weston.ini
NESTED_INI=/home/admin/.config/weston-nested-pub.ini

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
loginctl enable-linger admin 2>/dev/null || true

# Stop the admin user's production qdwin session first; otherwise
# Restart=on-failure relaunches weston between our pkill and our own
# outer's startup, racing the wayland-1 lockfile.
systemctl --machine=admin@.host --user stop \
    qdshell.service qdwin-compositor.service qdlocker.service \
    2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
pkill -9 -f "weston-terminal" 2>/dev/null || true
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

# --- stage qdshell + protocol XMLs ----------------------------------
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

# --- outer weston.ini -----------------------------------------------
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

# --- nested weston.ini -- publisher mode, PIPEWIRE-ONLY (the F6#2 fix) ---
# This mirrors the shipped tier2/weston.ini: no wayland-backend, so the
# inner weston creates NO host-output window on the outer. If pipewire-only
# fails to bring weston up the publisher-ready check below fails (exit 3) —
# that is the empirical gate codex flagged before trusting Approach A.
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

rm -f "$WLOG" "$NLOG"; touch "$WLOG" "$NLOG"; chown admin:admin "$WLOG" "$NLOG"

# --- start outer qdwin (with the S3d route-test enabled) ------------
cat >/home/admin/run-s3d-outer.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
export QDWIN_NESTED_S3D_TEST=1
exec weston \\
    --config=$INI \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s3d-outer.sh; chown admin:admin /home/admin/run-s3d-outer.sh
runuser -u admin -- nohup /home/admin/run-s3d-outer.sh >>"$WLOG" 2>&1 </dev/null &
OUTPID=$!

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'qdwin: shell loaded' "$WLOG" || {
    echo "FAIL: outer weston did not load qdwin-shell"
    tail -20 "$WLOG"
    exit 2
}
echo "PASS: outer qdwin started (S3d route-test enabled)"
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

# --- attach a peer so outer has a seat + paints ---------------------
runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 90 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s3d-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

# --- start nested weston (pipewire-only publisher) ------------------
cat >/home/admin/run-s3d-nested.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-1
export QDWIN_NESTED_MODE=1
export QDWIN_OUTER_DISPLAY=wayland-1
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --config=$NESTED_INI \\
    -Sqdwin-nested-pub \\
    --log=$NLOG
EOF
chmod +x /home/admin/run-s3d-nested.sh; chown admin:admin /home/admin/run-s3d-nested.sh
runuser -u admin -- nohup /home/admin/run-s3d-nested.sh >>"$NLOG" 2>&1 </dev/null &
NESTEDPID=$!

for i in $(seq 1 15); do
    sleep 1
    grep -q 'nested-mode publisher ready' "$NLOG" 2>/dev/null && break
done
if ! grep -q 'nested-mode publisher ready' "$NLOG"; then
    echo "FAIL: pipewire-only nested weston did not initialise publisher mode"
    echo "--- nested log ---"; tail -40 "$NLOG"
    pkill -9 -f run-s3d-nested 2>/dev/null || true
    pkill -9 -x weston 2>/dev/null || true
    exit 3
fi
echo "PASS: pipewire-only nested weston publisher mode ready"

# Capture the nested publisher pid for the no-phantom assertion. This pid is
# load-bearing: without it we cannot assert "no regular toplevel from the nested
# publisher", so an empty NPID is a HARD FAIL (not a silent skip) — otherwise the
# most important negative assertion in this lane would false-green.
# `|| true`: under `set -e -o pipefail` a missing pid line would otherwise abort
# the script on the failed grep BEFORE the intended hard-fail+diagnostics below.
NPID=$(grep -oE 'NESTED_MODE on; pid=[0-9]+' "$NLOG" | head -1 | grep -oE '[0-9]+' || true)
if [ -z "$NPID" ]; then
    echo "FAIL: could not determine nested publisher pid from '$NLOG'"
    echo "      (expected a 'qdwin: NESTED_MODE on; pid=N' line) — cannot run"
    echo "      the no-phantom assertion without it"
    echo "--- nested tail ---"; tail -30 "$NLOG"
    pkill -9 -x weston 2>/dev/null || true
    exit 3
fi
echo "nested publisher pid: $NPID"

if ! grep -q 'nested_manager bound' "$WLOG"; then
    echo "FAIL: outer never logged 'nested_manager bound'"
    tail -30 "$WLOG"
    pkill -9 -x weston 2>/dev/null || true
    exit 4
fi
echo "PASS: publisher bound qdwin_nested_manager_v1 on outer"

# --- spawn an inner client (weston-terminal) ------------------------
# advertise_toplevel fires on the outer here; the S3d route-test is then
# DEFERRED by qdwin to a one-shot timer (~500ms) so it runs after the next
# repaint (when weston_compositor_pick_view's view_list includes the proxy).
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=qdwin-nested-pub \
    nohup weston-terminal >/tmp/s3d-wt.log 2>&1 </dev/null &
WTPID=$!

for i in $(seq 1 15); do
    sleep 1
    grep -q 'qdwin/nested-proxy: created handle=' "$WLOG" 2>/dev/null && break
done
if ! grep -q 'qdwin/nested-proxy: created handle=' "$WLOG"; then
    echo "FAIL: outer never created the nested proxy"
    echo "--- outer tail ---"; tail -30 "$WLOG"
    echo "--- nested tail ---"; tail -30 "$NLOG"
    kill "$WTPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true
    exit 5
fi
echo "PASS: outer received advertise + created nested proxy"

# --- NO-PHANTOM regression assertion --------------------------------
# With a pipewire-only nested weston, the wayland-backend host-output
# window is gone, so NO regular toplevel_added line should carry the
# nested publisher pid. (The inner weston-terminal is shown as a PROXY,
# never as a regular outer toplevel.)
if grep -E "qdwin: toplevel_added handle=[0-9]+ uid=[0-9]+ pid=$NPID " "$WLOG"; then
    echo "FAIL: phantom regression — a regular toplevel_added came from the"
    echo "      nested publisher pid=$NPID (wayland-backend host-output window?)"
    grep -E "toplevel_added .* pid=$NPID " "$WLOG"
    kill "$WTPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true
    exit 6
fi
echo "PASS: no phantom host-output toplevel from the nested publisher (pid=$NPID)"

# --- S3d ROUTING assertion (outer side) -----------------------------
# Wait for the route-test log; qdwin fires it on a deferred one-shot timer
# (~500ms after advertise, post-repaint), so allow a few seconds.
for i in $(seq 1 10); do
    grep -q 'qdwin/nested-proxy: S3d route-test' "$WLOG" 2>/dev/null && break
    sleep 1
done
S3D_LINE=$(grep 'qdwin/nested-proxy: S3d route-test' "$WLOG" | head -1)
echo "S3d line: ${S3D_LINE:-<none>}"
# pick_matched=1 is the load-bearing assertion: it proves the REAL compositor
# picker (weston_compositor_pick_view) resolves to the proxy view — i.e. the
# phantom is genuinely gone — not merely that focus, once set, tracks. Require
# BOTH pick_matched=1 (picker resolves to proxy) and active_input_proxy_matched=1
# (the focus tracker then arms the QDNI forward).
if echo "$S3D_LINE" | grep -qE 'pick_matched=1 active_input_proxy_matched=1'; then
    echo "PASS: S3d route-test — pick_view resolved to the proxy + active_input_proxy armed"
else
    echo "FAIL: S3d route-test — pick_view did not resolve to the proxy and/or"
    echo "      active_input_proxy not armed (routing chain"
    echo "      pick_view->track_focus->active_input_proxy broken, or phantom present)"
    kill "$WTPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true
    exit 7
fi

# --- inner-side QDNI button decode (routed, not the S3b synthetic) --
for i in $(seq 1 10); do
    grep -qE 'qdwin/nested: button handle=[0-9]+ btn=0x110 state=1' "$NLOG" 2>/dev/null && break
    sleep 1
done
if grep -qE 'qdwin/nested: button handle=[0-9]+ btn=0x110 state=1' "$NLOG" && \
   grep -qE 'qdwin/nested: button handle=[0-9]+ btn=0x110 state=0' "$NLOG"; then
    echo "PASS: inner weston decoded the ROUTED QDNI button (press + release)"
else
    echo "FAIL: inner weston never decoded the routed QDNI button"
    echo "--- nested tail ---"; tail -30 "$NLOG"
    kill "$WTPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true
    exit 8
fi

# --- teardown -------------------------------------------------------
kill "$WTPID" 2>/dev/null || true
pkill -9 -f weston-terminal 2>/dev/null || true
kill "$NESTEDPID" 2>/dev/null || true
pkill -9 -f run-s3d-nested 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
kill "$OUTPID" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

echo
echo "PASS: §6.8 S3d nested-proxy input routing + no-phantom end-to-end"
