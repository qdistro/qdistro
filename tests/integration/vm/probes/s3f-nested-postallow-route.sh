#!/bin/bash
# §6.8 S3f — end-to-end nested-proxy TRANSITION lane: the SAME proxy driven
# through pending -> bind(stash) -> allow -> pickable + routes input (F6#2).
#
# s3d proves a proxy that was NEVER pending routes; s3e proves a pending+bound
# proxy stays UNPICKABLE. Neither drives the post-`allow` transition on a single
# proxy. This lane closes that gap WITHOUT a v9 qdshell/broker on the wire: the
# qdwin-side allow MECHANISM (qdwin_nested_proxy_apply_allow — the identical code
# the shell `nested_proxy_decision`=allow handler runs, incl. mechanism A's
# deferred curtain->pixel swap) already exists, and this lane drives that exact
# code path via an env-gated test hook.
#
# Scope note (codex GO/NO-GO, 2026-06-16): this is COMPOSITOR-SIDE post-allow
# transition coverage. The broker/qdshell WIRE-PATH authorization (a real
# qdshell calling broker CheckPermission and shipping the verdict over
# qdwin-shell-v1) is STILL required and remains the skipped Phase-3 lane (s24,
# SKIP_LEGACY_NESTED_QDSHELL) — this lane does NOT replace it.
#
# Mechanics: outer weston runs with
#   QDWIN_NESTED_BROKER_REQUIRED=1            (force pending, like s3e)
#   QDWIN_NESTED_S3D_TEST=1                   (the deferred pick/route oracle)
#   QDWIN_NESTED_S3D_ALLOW_AFTER_PENDING=1    (drive the post-allow transition)
# On advertise qdwin schedules a one-shot timer. FIRST fire (phase 0): logs the
# pending-check (pending_unpickable=1) on the SAME proxy whose pixel feed was
# bound (stashed) while pending, then applies the REAL allow and re-arms a
# second post-repaint timer. SECOND fire (phase 1): the proxy is no longer
# pending, so the visible routing branch runs and logs
#   S3d route-test ... pick_matched=1 active_input_proxy_matched=1
# and the inner weston decodes the routed QDNI button.
#
# Asserts, in transition order (the deterministic, ydotool-independent proof):
#   1. pixel feed STASHED while pending (mechanism A, swap deferred)
#   2. S3d pending-check pending_unpickable=1 (pending proxy not pickable)
#   3. S3d allow-after-pending applying allow (the transition fired)
#   4. nested_proxy_decision ... ALLOW core ran (holding_released via the helper)
#   5. S3d route-test pick_matched=1 active_input_proxy_matched=1 (now pickable
#      + routes) on the SAME handle
#   6. inner weston decoded the routed QDNI button (press + release)
#
# Exit codes: 0 PASS; 2 outer; 3 nested; 5 no/non-pending proxy; 6 phantom;
#   7 bind not deferred while pending; 8 pending proxy was pickable;
#   9 allow transition did not fire / proxy not released;
#   10 post-allow route-test did not resolve to the proxy;
#   11 inner never decoded the routed QDNI button.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s3f-weston-outer.log
NLOG=/home/admin/s3f-weston-nested.log
PFLOG=/home/admin/s3f-pixelfeed.log
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

# nested publisher: PIPEWIRE-ONLY (the F6#2 config), same as s3d/s3e.
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

# --- outer qdwin: broker-required (forces pending) + S3d route-test
#     + allow-after-pending (drives the post-allow transition) ----------
cat >/home/admin/run-s3f-outer.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
export QDWIN_NESTED_S3D_TEST=1
export QDWIN_NESTED_BROKER_REQUIRED=1
export QDWIN_NESTED_S3D_ALLOW_AFTER_PENDING=1
exec weston \\
    --config=$INI \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s3f-outer.sh; chown admin:admin /home/admin/run-s3f-outer.sh
runuser -u admin -- nohup /home/admin/run-s3f-outer.sh >>"$WLOG" 2>&1 </dev/null &
OUTPID=$!

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'qdwin: shell loaded' "$WLOG" || {
    echo "FAIL: outer weston did not load qdwin-shell"; tail -20 "$WLOG"; exit 2; }
echo "PASS: outer qdwin started (broker-required + S3d route-test + allow-after-pending)"
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 120 sdl-freerdp /v:127.0.0.1:3389 /cert:ignore /u:probe /p:probe \
        >/tmp/s3f-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

# --- nested publisher (pipewire-only) ----------------------------------
cat >/home/admin/run-s3f-nested.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-1
export QDWIN_NESTED_MODE=1
export QDWIN_OUTER_DISPLAY=wayland-1
export QDWIN_ALLOWED_UID=1000
exec weston --config=$NESTED_INI -Sqdwin-nested-pub --log=$NLOG
EOF
chmod +x /home/admin/run-s3f-nested.sh; chown admin:admin /home/admin/run-s3f-nested.sh
runuser -u admin -- nohup /home/admin/run-s3f-nested.sh >>"$NLOG" 2>&1 </dev/null &
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
    nohup weston-terminal >/tmp/s3f-wt.log 2>&1 </dev/null &
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
echo "proxy line: $PROXY_LINE"
if ! echo "$PROXY_LINE" | grep -q "pending=1"; then
    echo "FAIL: proxy is not pending despite QDWIN_NESTED_BROKER_REQUIRED=1"
    kill "$WTPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true; exit 5
fi
echo "PASS: outer created a PENDING nested proxy (broker-required)"
HANDLE=$(echo "$PROXY_LINE" | sed -n 's/.*created handle=\([0-9]*\).*/\1/p')

# no-phantom (pipewire-only nested), same invariant as s3d/s3e
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
# HOLD long enough for BOTH deferred fires (phase 0 ~0.5s + phase 1 ~0.5s after
# the post-repaint re-arm) + slack, so the stashed surface stays bound when the
# allow path performs the deferred curtain->pixel swap.
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 QDWIN_PIXELFEED_NO_PW=1 QDWIN_PIXELFEED_HOLD=20 \
    QDWIN_PIXELFEED_RGBA=0xff3030ff \
    nohup qdistro-nested-pixelfeed "$HANDLE" "$PWNODE" \
        >"$PFLOG" 2>&1 </dev/null &
PFPID=$!

# (1) Assert the swap was DEFERRED (mechanism A), NOT performed while pending.
for i in $(seq 1 12); do
    grep -q "bind_proxy_pixels handle=$HANDLE" "$WLOG" 2>/dev/null && break
    sleep 1
done
if grep -qE "bind_proxy_pixels handle=$HANDLE surface=.* \(curtain swapped for live feed\)" "$WLOG"; then
    echo "FAIL: bind_proxy_pixels SWAPPED the curtain while pending (exit 7)"
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

# (2) Phase-0 pending-check: the SAME proxy is NOT pickable while pending.
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

# (3)+(4) The allow transition fired and the REAL allow core ran (the helper's
# qdwin_toplevel_release_holding logs holding_released via the allow cause).
for i in $(seq 1 10); do
    grep -q "S3d allow-after-pending handle=$HANDLE" "$WLOG" 2>/dev/null && break
    sleep 1
done
if ! grep -qE "S3d allow-after-pending handle=$HANDLE stashed=1 — applying allow" "$WLOG"; then
    echo "FAIL: the post-allow transition never fired for handle=$HANDLE (exit 9)"
    echo "--- outer tail ---"; tail -30 "$WLOG"
    kill "$WTPID" "$PFPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true; exit 9
fi
for i in $(seq 1 10); do
    grep -qE "holding_released handle=$HANDLE via s3d-test/allow-after-pending" "$WLOG" 2>/dev/null && break
    sleep 1
done
if grep -qE "holding_released handle=$HANDLE via s3d-test/allow-after-pending" "$WLOG"; then
    echo "PASS: allow transition ran the REAL allow core (proxy released from held)"
else
    echo "FAIL: proxy was not released from held by the allow core (exit 9)"
    echo "--- outer tail ---"; tail -30 "$WLOG"
    kill "$WTPID" "$PFPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true; exit 9
fi

# (5) Phase-1 post-allow routing assertion on the SAME handle: now pickable +
# routes. Require BOTH pick_matched=1 (real picker resolves to the proxy) and
# active_input_proxy_matched=1 (the focus tracker armed the QDNI forward).
for i in $(seq 1 12); do
    grep -q "S3d route-test handle=$HANDLE" "$WLOG" 2>/dev/null && break
    sleep 1
done
S3D_LINE=$(grep "S3d route-test handle=$HANDLE" "$WLOG" | head -1)
echo "post-allow route-test: ${S3D_LINE:-<none>}"
if echo "$S3D_LINE" | grep -qE 'pick_matched=1 active_input_proxy_matched=1'; then
    echo "PASS: post-allow S3d route-test — SAME proxy now pickable + active_input_proxy armed"
else
    echo "FAIL: post-allow route-test did not resolve to the proxy (exit 10)"
    echo "--- outer tail ---"; tail -30 "$WLOG"
    kill "$WTPID" "$PFPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true; exit 10
fi

# (6) Inner-side QDNI button decode (the routed button, not a synthetic burst).
for i in $(seq 1 10); do
    grep -qE 'qdwin/nested: button handle=[0-9]+ btn=0x110 state=1' "$NLOG" 2>/dev/null && break
    sleep 1
done
if grep -qE 'qdwin/nested: button handle=[0-9]+ btn=0x110 state=1' "$NLOG" && \
   grep -qE 'qdwin/nested: button handle=[0-9]+ btn=0x110 state=0' "$NLOG"; then
    echo "PASS: inner weston decoded the ROUTED QDNI button (press + release)"
else
    echo "FAIL: inner weston never decoded the routed QDNI button (exit 11)"
    echo "--- nested tail ---"; tail -30 "$NLOG"
    kill "$WTPID" "$PFPID" 2>/dev/null || true; pkill -9 -x weston 2>/dev/null || true; exit 11
fi

# --- teardown ---
kill "$WTPID" "$PFPID" 2>/dev/null || true
pkill -9 -f weston-terminal 2>/dev/null || true
pkill -9 -f qdistro-nested-pixelfeed 2>/dev/null || true
kill "$NESTEDPID" 2>/dev/null || true
pkill -9 -f run-s3f-nested 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
kill "$OUTPID" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

echo
echo "PASS: §6.8 S3f nested-proxy pending->bind->allow->pickable+routes end-to-end"
