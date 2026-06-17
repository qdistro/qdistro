#!/bin/bash
# §6.8 S3b — input-sink decoder + per-toplevel inner-seat replay.
#
# Builds on s23 by enabling QDWIN_NESTED_S3B_TEST=1 in the OUTER
# environment, which makes the outer proxy_create after PING send a
# synthetic burst of motion / button / key / axis / focus packets to
# the per-toplevel input sink. The nested side decodes them, lazily
# initialises a per-toplevel weston_seat, and dispatches via
# notify_motion_absolute / notify_button / notify_key / notify_axis on
# that seat.
#
# Acceptance: nested log shows
#   - input-sink PING (S3 wire-format proven, regression check)
#   - inner-seat ready
#   - focus handle=N focused=1 + focused=0 (enter + leave)
#   - button handle=N btn=0x110 state=1 + state=0 (BTN_LEFT down/up)
#   - key handle=N key=30 state=1 + state=0 (KEY_A down/up)
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s25-weston-outer.log
NLOG=/home/admin/s25-weston-nested.log
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

# --- start outer qdwin (S3b test env on) -----------------------------
cat >/home/admin/run-s25-outer.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
export QDWIN_NESTED_S3B_TEST=1
exec weston \\
    --config=$INI \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s25-outer.sh; chown admin:admin /home/admin/run-s25-outer.sh
runuser -u admin -- nohup /home/admin/run-s25-outer.sh >>"$WLOG" 2>&1 </dev/null &
OUTPID=$!

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'qdwin: shell loaded' "$WLOG" || { echo "FAIL: outer not up"; exit 2; }
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true
echo "PASS: outer qdwin started (S3b synthetic burst armed)"

# --- attach SDL freerdp peer so outer paints + nested has a target ---
runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 90 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s25-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

# --- nested weston (publisher) ---------------------------------------
cat >/home/admin/run-s25-nested.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-1
export QDWIN_NESTED_MODE=1
export QDWIN_OUTER_DISPLAY=wayland-1
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --config=$NESTED_INI \\
    -Sqdwin-nested-pub-s25 \\
    --log=$NLOG
EOF
chmod +x /home/admin/run-s25-nested.sh; chown admin:admin /home/admin/run-s25-nested.sh
runuser -u admin -- nohup /home/admin/run-s25-nested.sh >>"$NLOG" 2>&1 </dev/null &
NESTEDPID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do
    grep -q 'nested-mode publisher ready' "$NLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'nested-mode publisher ready' "$NLOG" || {
    echo "FAIL: nested publisher did not start"; tail -20 "$NLOG"; exit 3
}
echo "PASS: nested publisher up"

# --- spawn weston-terminal (triggers proxy_create + S3b burst) -------
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=qdwin-nested-pub-s25 \
    nohup weston-terminal >/tmp/s25-wt.log 2>&1 </dev/null &
WTPID=$!

# Wait up to 30s for the burst to fire — first-run weston-terminal
# spawn on a fresh VM can be much slower than a warmed-up clone.
for i in $(seq 1 30); do
    sleep 1
    grep -q 'S3b synthetic burst sent' "$WLOG" 2>/dev/null && break
done

if grep -q 'S3b synthetic burst sent' "$WLOG"; then
    echo "PASS: §6.8 S3b outer sent synthetic event burst"
else
    echo "FAIL: outer did not log synthetic burst"
    echo "--- outer log tail ---"; tail -30 "$WLOG"
    exit 4
fi

# Poll for the LAST expected nested log line (focus_leave is the final
# packet in the burst). Replaces a fragile fixed sleep — first-time
# pipewire/dbus warm-up on a fresh VM can stretch dispatch latency.
LAST_PAT='focus handle=.* focused=0'
for i in $(seq 1 30); do
    grep -qE "$LAST_PAT" "$NLOG" 2>/dev/null && break
    sleep 1
done

# Required nested log lines (all 6 must appear).
need=(
    "input-sink PING handle="
    "inner-seat 'qdwin-nested-T"
    "focus handle=.* focused=1"
    "button handle=.* btn=0x110 state=1"
    "button handle=.* btn=0x110 state=0"
    "key handle=.* key=30 state=1"
    "key handle=.* key=30 state=0"
    "focus handle=.* focused=0"
)
for pat in "${need[@]}"; do
    if grep -qE "$pat" "$NLOG"; then
        echo "PASS: nested decoded — $pat"
    else
        echo "FAIL: nested log missing — $pat"
        echo "--- nested log (input-sink + dispatch lines) ---"
        grep -E 'input-sink|inner-seat|motion handle|button handle|key handle|focus handle|axis' "$NLOG"
        exit 5
    fi
done

# --- teardown -------------------------------------------------------
kill "$WTPID" 2>/dev/null || true
pkill -9 -f weston-terminal 2>/dev/null || true
kill "$NESTEDPID" 2>/dev/null || true
pkill -9 -f run-s25-nested 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
kill "$OUTPID" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

echo
echo "PASS: §6.8 S3b input-sink decoder + per-toplevel inner-seat replay end-to-end"
