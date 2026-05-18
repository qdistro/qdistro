#!/bin/bash
# §6.8 S4 — admin-broker authz gate end-to-end.
#
# Builds on s23 (publisher + outer proxy) by adding qdshell + broker
# to the outer side. The proxy starts in held layer (invisible)
# until qdshell receives `nested_proxy_pending`, calls broker
# CheckPermission, and ships the verdict via `nested_proxy_decision`.
#
# Two scenarios in one probe:
#
#   ALLOW path
#     1. Seed approval cache: action=qdistro.nested.advertise:
#        org_freedesktop_weston_wayland_terminal scope=forever value=true
#     2. Bring up outer + qdshell (v8) + broker.
#     3. Bring up nested + publisher + weston-terminal.
#     4. Outer log MUST contain:
#          - qdwin: nested_proxy_decision handle=N ALLOW
#          - qdwin: holding_released handle=N via nested_proxy_decision/allow
#
#   DENY path  (sub-process, separate weston-terminal advertise)
#     1. Seed cache row deny for action.
#     2. Same setup, second weston-terminal.
#     3. Outer log MUST contain:
#          - qdwin: nested_proxy_decision handle=M DENY
#          - qdwin/nested-proxy: destroy handle=M
#
# A 3rd "defer" path is left as a follow-up (requires racing the
# RequestPermission + verifying the proxy stays held).
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s24-weston-outer.log
NLOG=/home/admin/s24-weston-nested.log
SLOG=/home/admin/s24-qdshell.log
SOCK=/tmp/qdshell-s24.sock
INI=/home/admin/.config/weston.ini
NESTED_INI=/home/admin/.config/weston-nested-pub.ini
ACTION="qdistro.nested.advertise:org.freedesktop.weston.wayland-terminal"

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
loginctl enable-linger admin 2>/dev/null || true
if ! systemctl is-active --quiet qdistro-admin-broker.service; then
    echo "FAIL: broker not running"
    exit 1
fi

pkill -9 -x weston 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
pkill -9 -f weston-terminal 2>/dev/null || true
pkill -9 -f qdshell.py 2>/dev/null || true
rm -f "$SOCK"
sleep 1

# Fresh approval cache state for the action.
python3 - <<PYEOF
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
for row in c.list_all():
    if row["caller_uid"] == 1000 and row["action"] == "$ACTION":
        c.delete_by_id(row["id"])
# Seed allow.
c.store(1000, "$ACTION", "", "forever", True, 1000)
print(f"seeded allow row for {row['action'] if False else '$ACTION'}")
PYEOF

# Stage qdshell + nested protocol XML.
rm -rf /home/admin/qdshell
install -d -o admin -g admin /home/admin/qdshell
cp -r "$QDWIN_SRC/qdshell/." /home/admin/qdshell/
install -m 0644 "$QDWIN_SRC/qdwin/qdwin-shell-v1.xml" \
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
cat >/home/admin/run-s24-outer.sh <<EOF
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
chmod +x /home/admin/run-s24-outer.sh; chown admin:admin /home/admin/run-s24-outer.sh
runuser -u admin -- nohup /home/admin/run-s24-outer.sh >>"$WLOG" 2>&1 </dev/null &
OUTPID=$!

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'qdwin: shell loaded' "$WLOG" || {
    echo "FAIL: outer weston did not load qdwin-shell"
    tail -20 "$WLOG"; exit 2
}
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

# --- attach SDL freerdp peer so outer paints + nested has a target ---
runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 90 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s24-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

# --- start qdshell on the outer with broker integration --------------
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus" \
    nohup python3 /home/admin/qdshell/qdshell.py \
        --ctrl-socket="$SOCK" >>"$SLOG" 2>&1 </dev/null &
SHPID=$!
for i in 1 2 3 4 5 6 7 8; do
    [ -S "$SOCK" ] && break
    sleep 1
done
chmod a+rw "$SOCK" 2>/dev/null || true
echo "PASS: outer qdwin + qdshell up"

# --- start nested weston (publisher) ---------------------------------
cat >/home/admin/run-s24-nested.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-1
export QDWIN_NESTED_MODE=1
export QDWIN_OUTER_DISPLAY=wayland-1
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --config=$NESTED_INI \\
    -Sqdwin-nested-pub-s24 \\
    --log=$NLOG
EOF
chmod +x /home/admin/run-s24-nested.sh; chown admin:admin /home/admin/run-s24-nested.sh
runuser -u admin -- nohup /home/admin/run-s24-nested.sh >>"$NLOG" 2>&1 </dev/null &
NESTEDPID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do
    grep -q 'nested-mode publisher ready' "$NLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'nested-mode publisher ready' "$NLOG" || {
    echo "FAIL: nested publisher did not start"
    tail -30 "$NLOG"; exit 3
}

# --- ALLOW path: spawn weston-terminal -------------------------------
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=qdwin-nested-pub-s24 \
    nohup weston-terminal >/tmp/s24-wt-allow.log 2>&1 </dev/null &
WTPID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do
    grep -q 'nested_proxy_decision handle=.* ALLOW' "$WLOG" 2>/dev/null && break
    sleep 1
done

if grep -q 'nested_proxy_decision handle=.* ALLOW' "$WLOG"; then
    echo "PASS: §6.8 S4 ALLOW path — broker allowed nested-proxy"
else
    echo "FAIL: outer never logged ALLOW for the nested proxy"
    echo "--- outer log tail ---"; tail -30 "$WLOG"
    echo "--- qdshell log tail ---"; tail -30 "$SLOG"
    exit 4
fi
if grep -q 'holding_released handle=.* via nested_proxy_decision/allow' "$WLOG"; then
    echo "PASS: §6.8 S4 ALLOW released proxy from held to normal"
else
    echo "FAIL: ALLOW did not release proxy from held layer"
    exit 5
fi

# --- DENY path: re-seed deny then second weston-terminal -------------
kill "$WTPID" 2>/dev/null || true
pkill -9 -f weston-terminal 2>/dev/null || true
sleep 1

python3 - <<PYEOF
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
for row in c.list_all():
    if row["caller_uid"] == 1000 and row["action"] == "$ACTION":
        c.delete_by_id(row["id"])
# Seed deny.
c.store(1000, "$ACTION", "", "forever", False, 1000)
PYEOF

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=qdwin-nested-pub-s24 \
    nohup weston-terminal >/tmp/s24-wt-deny.log 2>&1 </dev/null &
WT2PID=$!
for i in 1 2 3 4 5 6 7 8 9 10; do
    grep -q 'nested_proxy_decision handle=.* DENY' "$WLOG" 2>/dev/null && break
    sleep 1
done

if grep -q 'nested_proxy_decision handle=.* DENY' "$WLOG"; then
    echo "PASS: §6.8 S4 DENY path — broker denied nested-proxy"
else
    echo "FAIL: outer never logged DENY for the nested proxy"
    echo "--- outer log tail ---"; tail -30 "$WLOG"
    echo "--- qdshell log tail ---"; tail -30 "$SLOG"
    exit 6
fi
if grep -q 'qdwin/nested-proxy: destroy handle=' "$WLOG"; then
    echo "PASS: §6.8 S4 DENY destroyed proxy"
else
    echo "FAIL: DENY did not destroy proxy"
    exit 7
fi

# --- teardown -------------------------------------------------------
kill "$WT2PID" 2>/dev/null || true
pkill -9 -f weston-terminal 2>/dev/null || true
kill "$NESTEDPID" 2>/dev/null || true
pkill -9 -f run-s24-nested 2>/dev/null || true
kill "$SHPID" 2>/dev/null || true
pkill -9 -f qdshell.py 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
kill "$OUTPID" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

# Reset approval cache to a clean slate so re-runs don't see stale rows.
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
echo "PASS: §6.8 S4 admin-broker gate (allow + deny) end-to-end"
