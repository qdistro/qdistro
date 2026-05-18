#!/bin/bash
# §6.5 S4 revoke-drives-teardown probe (broker present).
#
# End-to-end:
# 1. Confirm qdistro-admin-broker.service is up (install if missing).
# 2. Seed an approval cache row for uid 1000 / action
#    "qdistro.view-stream.subscribe:unknown" (the action weston-terminal
#    ends up with given empty app_id — see _broker_slug in qdshell.py).
# 3. Bring up weston + qdshell + weston-terminal (same launch shape as
#    s4-broker-gate-probe.sh).
# 4. ctrl-socket subscribe: `stream <h> label ...` — CheckPermission
#    hits the cache row, returns "allow", so subscribe proceeds.
# 5. Assert: ctrl `streams` lists the new handle.
# 6. Revoke via `dbus-send` as root — broker emits `ApprovalRevoked`.
# 7. Wait ~500 ms for the signal → queue → main-loop drain path.
# 8. Assert: ctrl `streams` is empty AND qdshell log contains
#    "revoke tearing down handle=".
#
# Prints "PASS: revoke teardown end-to-end" on success, "FAIL: …" on
# any step.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdwin-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s4-revoke-weston.log
SLOG=/home/admin/s4-revoke-qdshell.log
SOCK=/tmp/qdshell-s4-revoke.sock
INI=/home/admin/.config/weston.ini
ACTION="qdistro.view-stream.subscribe:unknown"

# 0. Preconditions.
pgrep -x pipewire >/dev/null || { echo "FAIL: pipewire not running"; exit 1; }
if ! systemctl is-active --quiet qdistro-admin-broker.service; then
    echo "FAIL: broker not running; install via install-broker-for-qdwin.sh"
    exit 1
fi

# Clean stale state.
pkill -9 -x weston 2>/dev/null || true
pkill -9 weston-terminal 2>/dev/null || true
pkill -9 -f qdshell.py 2>/dev/null || true
pkill -9 -f qdistro-forward 2>/dev/null || true
rm -f "$SOCK"
sleep 1

# 1. Seed the approval cache row so CheckPermission returns "allow"
#    without an admin prompt (and thus a row exists to revoke later).
#    Scope="forever" → match_kind="always" → match_value="" (exe ignored).
#    qdshell runs as admin=1000; that's the caller_uid broker peers.
python3 - <<'PYEOF'
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
# Clean first so consecutive runs don't accrete rows.
for row in c.list_all():
    if row["caller_uid"] == 1000 and \
       row["action"] == "qdistro.view-stream.subscribe:unknown":
        c.delete_by_id(row["id"])
c.store(1000, "qdistro.view-stream.subscribe:unknown", "",
        "forever", True, 1000)
# Print the id of the freshly-stored row for the revoke step.
rid = c.list_all()[0]["id"]
print(f"SEEDED_ID={rid}")
PYEOF

SEEDED_ID=$(python3 - <<'PYEOF'
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
for row in c.list_all():
    if row["caller_uid"] == 1000 and \
       row["action"] == "qdistro.view-stream.subscribe:unknown":
        print(row["id"])
        break
PYEOF
)
[ -z "$SEEDED_ID" ] && { echo "FAIL: could not read back seeded row id"; exit 2; }
echo "seeded approval id=$SEEDED_ID"

# 2. Sync qdshell + refresh protocol. (The probe assumes a prior sync
#    has already landed qdwin-src; we only refresh qdshell since
#    that's what changes for the listener.)
rm -rf /home/admin/qdshell
install -d -o admin -g admin /home/admin/qdshell
cp -r "$QDWIN_SRC/qdshell/." /home/admin/qdshell/
install -m 0644 "$QDWIN_SRC/qdwin/qdwin-shell-v1.xml" \
    /home/admin/qdshell/qdwin-shell-v1.xml
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
num-outputs=2
EOF
chown -R admin:admin /home/admin/.config

rm -f "$WLOG" "$SLOG"
touch "$WLOG" "$SLOG"
chown admin:admin "$WLOG" "$SLOG"

cat >/home/admin/run-s4-revoke-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s4-revoke-weston.sh
chown admin:admin /home/admin/run-s4-revoke-weston.sh

runuser -u admin -- nohup /home/admin/run-s4-revoke-weston.sh >>"$WLOG" 2>&1 </dev/null &
for _ in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

# 3. Start qdshell with the revoke listener enabled (default).
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    nohup python3 /home/admin/qdshell/qdshell.py \
        --ctrl-socket=$SOCK >>$SLOG 2>&1 </dev/null &
for _ in 1 2 3 4 5; do
    [ -S "$SOCK" ] && break
    sleep 1
done

runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    nohup weston-terminal >/dev/null 2>&1 </dev/null &
sleep 3

# 4. Subscribe.
HANDLE=$(echo "list" | socat - UNIX-CONNECT:$SOCK | awk '/^tl /{print $2; exit}')
[ -z "$HANDLE" ] && { echo "FAIL: no toplevel"; exit 3; }
echo "handle=$HANDLE"

REPLY=$(echo "stream $HANDLE revoke-test 640 480 0" | \
    socat - UNIX-CONNECT:$SOCK)
echo "stream reply: $REPLY"
case "$REPLY" in
    ok*stream*awaiting) ;;
    *)
        echo "FAIL: subscribe did not succeed (broker should have allowed)"
        tail -30 "$SLOG"
        exit 4
        ;;
esac

# 5. Confirm the stream is live from qdshell's view.
STREAMS_BEFORE=$(echo "streams" | socat - UNIX-CONNECT:$SOCK)
echo "streams before revoke:"
echo "$STREAMS_BEFORE"
if ! echo "$STREAMS_BEFORE" | grep -q "stream $HANDLE action=$ACTION"; then
    echo "FAIL: streams-before did not list the new handle"
    exit 5
fi

# 6. Revoke the cache row via dbus-send (as root).
dbus-send --system --print-reply \
    --dest=com.qdistro.AdminBroker1 /com/qdistro/AdminBroker1 \
    com.qdistro.AdminBroker1.RevokeApproval "int32:$SEEDED_ID" \
    >/dev/null || { echo "FAIL: RevokeApproval dbus call failed"; exit 6; }
echo "RevokeApproval($SEEDED_ID) sent"

# 7. Wait for signal → queue → drain round-trip. drain_revoke_queue
#    runs once per select tick (0.25s cadence), so 1s is plenty.
sleep 1

# 8. Confirm teardown.
STREAMS_AFTER=$(echo "streams" | socat - UNIX-CONNECT:$SOCK)
echo "streams after revoke:"
echo "$STREAMS_AFTER"
if echo "$STREAMS_AFTER" | grep -q "^stream "; then
    echo "FAIL: stream still live after revoke"
    echo "--- qdshell log tail ---"
    tail -30 "$SLOG"
    exit 7
fi

if ! grep -q "revoke tearing down handle=$HANDLE action='$ACTION'" "$SLOG"; then
    echo "FAIL: expected 'revoke tearing down' log line not found"
    echo "--- qdshell log tail ---"
    tail -40 "$SLOG"
    exit 8
fi

echo "PASS: revoke teardown end-to-end"
