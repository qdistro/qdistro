#!/bin/bash
# §spec/25 Phase-2 — admin-approval-app smoke for the new
# layered-identity surface + escalating urgent banner.
#
# Verifies:
#   1. broker.GetPending now returns exe_sha256 / selinux_label /
#      cgroup keys (broker-side capture).
#   2. The approval app, launched headless under QT_QPA_PLATFORM=
#      offscreen, logs "urgent banner shown" within a few ticks of
#      a request whose details["urgency"]="urgent" lands.
#   3. After we deny that urgent request, the app logs
#      "urgent banner cleared".
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdwin-src}

systemctl is-active --quiet qdistro-admin-broker.service || {
    systemctl start qdistro-admin-broker.service
    sleep 1
}
echo "PASS: broker active"

# 1. GetPending shape: inject one request as admin and verify the new
# keys appear in the returned dict. PEER_EXE has to point at a real
# /proc-readable binary or the broker's _read_proc_layered will return
# empty strings — running through `runuser -u admin -- python3` makes
# python3 itself the peer, which has a populated /proc/<pid>/exe.
runuser -u admin -- python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
rid = iface.RequestPermission('qdistro.smoke.layered',
                              dbus.Dictionary({'note': dbus.String('s30 layered')},
                                              signature='sv'))
print('rid=', rid)
"
sleep 1

LAYERED_OK=$(python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
rows = iface.GetPending()
ok = False
for r in rows:
    keys = set(str(k) for k in r.keys())
    if {'exe_sha256', 'selinux_label', 'cgroup'}.issubset(keys):
        sha = str(r['exe_sha256'])
        if len(sha) == 64 and all(c in '0123456789abcdef' for c in sha):
            ok = True; break
print('YES' if ok else 'NO')
")
if [ "$LAYERED_OK" = "YES" ]; then
    echo "PASS: GetPending exposes layered identity (sha256 + selinux + cgroup)"
else
    echo "FAIL: GetPending missing layered keys or sha256 not 64-char hex"
    exit 2
fi

# Drain that pending so the urgent test starts clean.
DRAIN_ID=$(python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
pending = iface.GetPending()
if pending: print(int(pending[0]['id']))
")
if [ -n "$DRAIN_ID" ]; then
    runuser -u admin -- python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
iface.DecideRequest(dbus.Int32($DRAIN_ID), 'deny', 'once')
" >/dev/null
fi

# 2. Spawn approval app headless. SNI watcher isn't needed for the
# banner check — the urgent-banner code path lives entirely inside
# the QtWidgets refresh loop.
APP_PATH=""
for p in /usr/local/bin/qdistro-admin-approval-app \
         /usr/share/qdshell/qdistro-admin-approval-app.py; do
    [ -f "$p" ] && APP_PATH="$p" && break
done
if [ -z "$APP_PATH" ]; then
    install -m 0755 \
        "$QDWIN_SRC/qdshell/qdistro-admin-approval-app.py" \
        /usr/local/bin/qdistro-admin-approval-app
    APP_PATH=/usr/local/bin/qdistro-admin-approval-app
fi
echo "PASS: approval app at $APP_PATH"

LOG=/tmp/s30-approval-app.log
PID_FILE=/tmp/s30-approval-app.pid
rm -f "$LOG" "$PID_FILE"

runuser -u admin -- bash <<RUSH
export XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
export QT_QPA_PLATFORM=offscreen
export QDISTRO_APPROVAL_APP_AUTOSHOW=1
setsid python3 $APP_PATH </dev/null >$LOG 2>&1 &
echo \$! > $PID_FILE
disown || true
RUSH
sleep 4
APP_PID=$(cat "$PID_FILE")
if ! kill -0 "$APP_PID" 2>/dev/null; then
    APP_PID=$(pgrep -u admin -f 'qdistro-admin-approval-app' | head -1)
fi
if [ -z "$APP_PID" ] || ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "FAIL: approval app exited prematurely"
    cat "$LOG"
    exit 3
fi
echo "PASS: approval app running (pid=$APP_PID)"

# 3. Inject an urgent request.
runuser -u admin -- python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
iface.RequestPermission('qdistro.smoke.urgent',
                        dbus.Dictionary(
                            {'urgency': dbus.String('urgent'),
                             'note': dbus.String('s30 urgent')},
                            signature='sv'))
print('requested', flush=True)
" >/dev/null

# Banner refresh runs from the 1 Hz on_tick; allow up to 8s.
for i in $(seq 1 8); do
    grep -q 'urgent banner shown' "$LOG" 2>/dev/null && break
    sleep 1
done
if grep -q 'urgent banner shown' "$LOG"; then
    HEAD_LINE=$(grep 'urgent banner shown' "$LOG" | tail -1)
    echo "PASS: urgent banner shown — $HEAD_LINE"
else
    echo "FAIL: urgent banner did not log 'shown' within 8s"
    tail -30 "$LOG"
    kill "$APP_PID" 2>/dev/null || true
    exit 4
fi

# 4. Deny the urgent request -> banner should clear.
URGENT_ID=$(python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
for r in iface.GetPending():
    print(int(r['id'])); break
")
if [ -z "$URGENT_ID" ]; then
    echo "FAIL: urgent request vanished before we could decide it"
    kill "$APP_PID" 2>/dev/null || true
    exit 5
fi
runuser -u admin -- python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
iface.DecideRequest(dbus.Int32($URGENT_ID), 'deny', 'once')
" >/dev/null

for i in $(seq 1 8); do
    grep -q 'urgent banner cleared' "$LOG" 2>/dev/null && break
    sleep 1
done
if grep -q 'urgent banner cleared' "$LOG"; then
    echo "PASS: urgent banner cleared after queue drained"
else
    echo "FAIL: urgent banner did not clear within 8s"
    tail -30 "$LOG"
    kill "$APP_PID" 2>/dev/null || true
    exit 6
fi

kill "$APP_PID" 2>/dev/null || true
sleep 0.5

echo "PASS: spec/25 §Phase-2 layered identity + urgent banner end-to-end"
