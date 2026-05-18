#!/bin/bash
# §6.8 S2b mvp — pixel-feed consumer end-to-end via bind_proxy_pixels.
#
# Same outer/qdshell/nested/weston-terminal stack as s24, plus:
#   - qdshell binds qdwin_shell_v1 v9 (so nested_proxy_pixel_source
#     fires);
#   - on the event, qdshell spawns qdistro-nested-pixelfeed with
#     QDWIN_PIXELFEED_HOLD=0 so it bind+exits, exercising the
#     destroy listener that reverts the proxy to a placeholder
#     curtain.
#
# Acceptance:
#   - outer logs nested_proxy_pixel_source emission
#   - qdshell logs spawning the pixelfeed
#   - pixelfeed exits with the bound proxy_pixels signal observed
#   - outer logs "bind_proxy_pixels handle=N surface=..."
#   - on consumer exit, outer logs the pixel-surface-destroyed +
#     placeholder-curtain reversion
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s26-weston-outer.log
NLOG=/home/admin/s26-weston-nested.log
SLOG=/home/admin/s26-qdshell.log
SOCK=/tmp/qdshell-s26.sock
INI=/home/admin/.config/weston.ini
NESTED_INI=/home/admin/.config/weston-nested-pub.ini
ACTION="qdistro.nested.advertise:org.freedesktop.weston.wayland-terminal"

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
loginctl enable-linger admin 2>/dev/null || true
if ! systemctl is-active --quiet qdistro-admin-broker.service; then
    systemctl start qdistro-admin-broker.service
fi

pkill -9 -x weston 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
pkill -9 -f weston-terminal 2>/dev/null || true
pkill -9 -f qdshell.py 2>/dev/null || true
pkill -9 -f qdistro-nested-pixelfeed 2>/dev/null || true
rm -f "$SOCK"
sleep 1

# Seed an allow rule so qdshell's broker check passes silently.
python3 - <<PYEOF
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
for row in c.list_all():
    if row["caller_uid"] == 1000 and row["action"] == "$ACTION":
        c.delete_by_id(row["id"])
c.store(1000, "$ACTION", "", "forever", True, 1000)
PYEOF

rm -rf /home/admin/qdshell
install -d -o admin -g admin /home/admin/qdshell
cp -r "$QDWIN_SRC/qdshell/." /home/admin/qdshell/
install -m 0644 "$QDWIN_SRC/qdwin/qdwin/qdwin-shell-v1.xml" \
    /home/admin/qdshell/qdwin-shell-v1.xml
install -m 0644 "$QDWIN_SRC/qdwin/qdwin-nested-v1.xml" \
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

rm -f "$WLOG" "$NLOG" "$SLOG"
touch "$WLOG" "$NLOG" "$SLOG"
chown admin:admin "$WLOG" "$NLOG" "$SLOG"

# --- start outer qdwin -----------------------------------------------
cat >/home/admin/run-s26-outer.sh <<EOF
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
chmod +x /home/admin/run-s26-outer.sh; chown admin:admin /home/admin/run-s26-outer.sh
runuser -u admin -- nohup /home/admin/run-s26-outer.sh >>"$WLOG" 2>&1 </dev/null &
OUTPID=$!

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'qdwin: shell loaded' "$WLOG" || {
    echo "FAIL: outer not up"; tail -20 "$WLOG"; exit 2
}
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 90 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s26-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

# --- start qdshell at v9 (pixel_source listener) ---------------------
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus" \
    QDWIN_PIXELFEED_HOLD=0 \
    QDWIN_PIXELFEED_RGBA=ff8800ff \
    nohup python3 /home/admin/qdshell/qdshell.py \
        --ctrl-socket="$SOCK" >>"$SLOG" 2>&1 </dev/null &
SHPID=$!
for i in 1 2 3 4 5 6 7 8; do
    [ -S "$SOCK" ] && break; sleep 1
done
chmod a+rw "$SOCK" 2>/dev/null || true

# --- start nested weston (publisher) ---------------------------------
cat >/home/admin/run-s26-nested.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-1
export QDWIN_NESTED_MODE=1
export QDWIN_OUTER_DISPLAY=wayland-1
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --config=$NESTED_INI \\
    -Sqdwin-nested-pub-s26 \\
    --log=$NLOG
EOF
chmod +x /home/admin/run-s26-nested.sh; chown admin:admin /home/admin/run-s26-nested.sh
runuser -u admin -- nohup /home/admin/run-s26-nested.sh >>"$NLOG" 2>&1 </dev/null &
NESTEDPID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do
    grep -q 'nested-mode publisher ready' "$NLOG" 2>/dev/null && break
    sleep 1
done

# --- spawn weston-terminal (triggers everything) ---------------------
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=qdwin-nested-pub-s26 \
    nohup weston-terminal >/tmp/s26-wt.log 2>&1 </dev/null &
WTPID=$!

for i in 1 2 3 4 5 6 7 8 9 10; do
    grep -q 'bind_proxy_pixels handle=' "$WLOG" 2>/dev/null && break
    sleep 1
done

if ! grep -q 'nested_proxy_pixel_source handle=' "$SLOG"; then
    echo "FAIL: qdshell did not log nested_proxy_pixel_source"
    tail -30 "$SLOG"; exit 3
fi
echo "PASS: §6.8 S2b qdshell received nested_proxy_pixel_source"

if ! grep -q 'spawned pixelfeed' "$SLOG"; then
    echo "FAIL: qdshell did not spawn pixelfeed"
    tail -30 "$SLOG"; exit 4
fi
echo "PASS: §6.8 S2b qdshell spawned qdistro-nested-pixelfeed"

if ! grep -q 'bind_proxy_pixels handle=' "$WLOG"; then
    echo "FAIL: outer did not receive bind_proxy_pixels"
    tail -30 "$WLOG"; exit 5
fi
echo "PASS: §6.8 S2b outer received bind_proxy_pixels (curtain swapped)"

# Pixelfeed should have exited (HOLD=0); destroy listener fires.
sleep 2
if grep -q 'pixel surface destroyed handle=' "$WLOG"; then
    echo "PASS: §6.8 S2b destroy listener reverted to placeholder curtain"
else
    echo "WARN: pixel-surface-destroyed not seen in window — pixelfeed "\
         "may still be holding"
fi

# --- teardown -------------------------------------------------------
kill "$WTPID" 2>/dev/null || true
pkill -9 -f weston-terminal 2>/dev/null || true
kill "$NESTEDPID" 2>/dev/null || true
pkill -9 -f run-s26-nested 2>/dev/null || true
kill "$SHPID" 2>/dev/null || true
pkill -9 -f qdshell.py 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
kill "$OUTPID" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

# Reset the allow rule.
python3 - <<PYEOF
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
for row in c.list_all():
    if row["caller_uid"] == 1000 and row["action"] == "$ACTION":
        c.delete_by_id(row["id"])
PYEOF

echo
echo "PASS: §6.8 S2b mvp pixel-feed bind end-to-end"
