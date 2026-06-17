#!/bin/bash
# §6.8 S3c — always-active keyboard grab encodes QDNI from real
# keyboard events.
#
# Same outer/nested/weston-terminal stack as s25 (S3b), but the outer
# enables QDWIN_NESTED_S3C_TEST=1 instead. After the proxy_create +
# input-sink connect, the outer:
#   - forces qdwin->active_input_proxy = the new proxy_tl (test
#     affordance — bypasses the pointer track_focus path, since
#     headless RDP makes simulating real pointer-on-view fiddly);
#   - sends focus_enter on the proxy's QDNI sink;
#   - calls notify_key on the first weston_seat with a keyboard,
#     which routes through weston_keyboard.default_grab.interface ==
#     qdwin_proxy_default_keyboard_grab_iface — i.e. our grab. Our
#     grab.key delegates to weston_keyboard_send_key (no-op without
#     a focused client) AND emits a QDNI key packet via
#     qdwin_nested_input_sink_send_key on the active proxy.
#   - sends focus_leave + restores active_input_proxy.
#
# The acceptance criterion is the *nested* side decoding key=31
# (KEY_S, distinct from S3b's KEY_A=30) — that proves the path from
# notify_key → libweston-routes-via-default-grab → our_iface->key →
# QDNI packet → nested decoder → notify_key on inner seat.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s28-weston-outer.log
NLOG=/home/admin/s28-weston-nested.log
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
pkill -9 -f weston-terminal 2>/dev/null || true
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

cat >"$NESTED_INI" <<EOF
[core]
shell=qdwin-shell.so
backend=wayland-backend.so,pipewire-backend.so
require-outputs=any
idle-time=0

[shell]
locking=false

[output]
name=WL1
mode=800x600

[pipewire]
num-outputs=8
EOF
chown admin:admin "$NESTED_INI"

rm -f "$WLOG" "$NLOG"; touch "$WLOG" "$NLOG"; chown admin:admin "$WLOG" "$NLOG"

# --- start outer qdwin (S3c test env on) -----------------------------
cat >/home/admin/run-s28-outer.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
export QDWIN_NESTED_S3C_TEST=1
exec weston \\
    --config=$INI \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s28-outer.sh; chown admin:admin /home/admin/run-s28-outer.sh
runuser -u admin -- nohup /home/admin/run-s28-outer.sh >>"$WLOG" 2>&1 </dev/null &
OUTPID=$!

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'qdwin: shell loaded' "$WLOG" || { echo "FAIL: outer not up"; exit 2; }
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true
echo "PASS: outer qdwin started (S3c keyboard-grab armed)"

if grep -q 'default keyboard grab installed on seat' "$WLOG"; then
    echo "PASS: §6.8 S3c default keyboard grab installed on a seat"
else
    echo "WARN: outer did not log keyboard-grab install yet — keyboard "
    echo "      may arrive only after RDP peer connects."
fi

# --- attach SDL freerdp peer so the RDP backend gets its keyboard ----
runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 90 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s28-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

# Wait for the keyboard-grab install log line — RDP backend adds the
# keyboard cap AFTER seat creation, so this should fire once peer is up.
for i in 1 2 3 4 5 6; do
    grep -q 'default keyboard grab installed on seat' "$WLOG" && break
    sleep 1
done
if ! grep -q 'default keyboard grab installed on seat' "$WLOG"; then
    echo "FAIL: keyboard-grab install log never appeared"
    grep -E 'seat|keyboard' "$WLOG" | tail -10
    exit 3
fi
echo "PASS: §6.8 S3c keyboard-grab install fired post-RDP-peer"

# --- nested weston (publisher) ---------------------------------------
cat >/home/admin/run-s28-nested.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-1
export QDWIN_NESTED_MODE=1
export QDWIN_OUTER_DISPLAY=wayland-1
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --config=$NESTED_INI \\
    -Sqdwin-nested-pub-s28 \\
    --log=$NLOG
EOF
chmod +x /home/admin/run-s28-nested.sh; chown admin:admin /home/admin/run-s28-nested.sh
runuser -u admin -- nohup /home/admin/run-s28-nested.sh >>"$NLOG" 2>&1 </dev/null &
NESTEDPID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do
    grep -q 'nested-mode publisher ready' "$NLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'nested-mode publisher ready' "$NLOG" || {
    echo "FAIL: nested publisher did not start"; tail -20 "$NLOG"; exit 4
}
echo "PASS: nested publisher up"

# --- spawn weston-terminal (triggers proxy_create + S3c burst) -------
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=qdwin-nested-pub-s28 \
    nohup weston-terminal >/tmp/s28-wt.log 2>&1 </dev/null &
WTPID=$!

# Wait up to 30s for the burst to fire — first-run weston-terminal
# spawn on a fresh VM can be much slower than a warmed-up clone.
for i in $(seq 1 30); do
    sleep 1
    grep -q 'S3c keyboard-grab burst' "$WLOG" 2>/dev/null && break
done

if ! grep -q 'S3c keyboard-grab burst' "$WLOG"; then
    echo "FAIL: outer did not log S3c keyboard-grab burst"
    echo "--- outer log tail ---"; tail -30 "$WLOG"
    exit 5
fi
echo "PASS: §6.8 S3c outer drove notify_key through default keyboard grab"

if grep -qE 'S3c keyboard-grab burst handle=[0-9]+ dispatched=1' "$WLOG"; then
    echo "PASS: §6.8 S3c notify_key reached at least one seat keyboard"
else
    echo "FAIL: dispatched=0 — no seat had a keyboard"
    grep 'S3c keyboard-grab burst' "$WLOG"
    exit 6
fi

# Poll for the LAST expected nested log line (key release). Replaces
# a fragile fixed sleep — first-time pipewire/dbus warm-up on a fresh
# VM can stretch QDNI dispatch latency.
LAST_PAT='key handle=.* key=31 state=0'
for i in $(seq 1 30); do
    grep -qE "$LAST_PAT" "$NLOG" 2>/dev/null && break
    sleep 1
done

# Acceptance: nested decoded a key whose origin was the keyboard grab.
# We use KEY_S (31) so it's distinct from S3b's KEY_A (30) burst.
need=(
    "input-sink PING handle="
    "key handle=.* key=31 state=1"
    "key handle=.* key=31 state=0"
)
for pat in "${need[@]}"; do
    if grep -qE "$pat" "$NLOG"; then
        echo "PASS: nested decoded — $pat"
    else
        echo "FAIL: nested log missing — $pat"
        echo "--- nested log (key + input-sink + focus lines) ---"
        grep -E 'input-sink|inner-seat|key handle|focus handle' "$NLOG"
        exit 7
    fi
done

# --- teardown -------------------------------------------------------
kill "$WTPID" 2>/dev/null || true
pkill -9 -f weston-terminal 2>/dev/null || true
kill "$NESTEDPID" 2>/dev/null || true
pkill -9 -f run-s28-nested 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
kill "$OUTPID" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

echo
echo "PASS: §6.8 S3c keyboard-grab end-to-end"
