#!/bin/bash
# §6.6 follow-up — admin-approval-app SNI tray badge smoke.
#
# Per spec/25 + task-013 decision #9: the approval app must show a
# permanent tray icon with a pending-count badge. This verifies the
# SNI side of the contract:
#   1. Admin's session bus is up (loginctl enable-linger).
#   2. qdshell's SNI watcher (modules/tray.py) is reachable when we
#      exercise it standalone.
#   3. The approval app, launched with QT_QPA_PLATFORM=offscreen, claims
#      its well-known org.kde.StatusNotifierItem-<pid>-1 bus name AND
#      calls RegisterStatusNotifierItem on the watcher.
#   4. Tooltip / Title properties reflect the pending-count after a
#      RequestPermission injection.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdwin-src}

# 1. Broker up (we need it for both GetPending and the "inject pending"
# path).
systemctl is-active --quiet qdistro-admin-broker.service || {
    systemctl start qdistro-admin-broker.service
    sleep 1
}
echo "PASS: broker active"

# 2. Start a minimal SNI watcher as admin (admin). qdshell's
# modules/tray.py is the production implementation — spawn just it in
# a small harness so we don't need a full qdshell running.
runuser -u admin -- bash -c '
export XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
python3 - <<PY >/tmp/watcher.log 2>&1 &
import sys, time
sys.path.insert(0, "/usr/share/qdshell")
from modules.tray import install_tray
import dbus.mainloop.glib, dbus
from gi.repository import GLib
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
state = {}
install_tray(state)
print("watcher up; tray=", state.get("tray"), flush=True)
loop = GLib.MainLoop()
loop.run()
PY
echo $! > /tmp/watcher.pid
'
sleep 2
if kill -0 "$(cat /tmp/watcher.pid)" 2>/dev/null; then
    echo "PASS: SNI watcher alive (pid=$(cat /tmp/watcher.pid))"
else
    echo "FAIL: SNI watcher did not start"
    cat /tmp/watcher.log
    exit 1
fi

# 3. Launch the approval app headless. Qt needs a platform plugin —
# "offscreen" renders to a pixmap, no compositor needed.
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

# Use a HEREDOC under runuser to avoid the quoting gymnastics that
# kill the pid-tracking in a `bash -c` flow (empirically: the PID
# written under that shape points at the runuser wrapper, which exits
# while python is still booting under offscreen Qt).
runuser -u admin -- bash <<RUSH
export XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
export QT_QPA_PLATFORM=offscreen
export QDISTRO_APPROVAL_APP_AUTOSHOW=0
setsid python3 $APP_PATH </dev/null >/tmp/approval-app.log 2>&1 &
echo \$! > /tmp/approval-app.pid
disown || true
RUSH
sleep 4
APP_PID=$(cat /tmp/approval-app.pid)
if ! kill -0 "$APP_PID" 2>/dev/null; then
    # setsid-under-runuser sometimes records the setsid pid which
    # exits after forking into a new session. Look up by name+owner.
    APP_PID=$(pgrep -u admin -f 'qdistro-admin-approval-app' | head -1)
fi
if [ -z "$APP_PID" ] || ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "FAIL: approval app exited prematurely"
    cat /tmp/approval-app.log
    exit 2
fi
echo "PASS: approval app running (pid=$APP_PID)"

# 4. Watcher should now see a registered item matching our pid.
ITEM_NAME=$(runuser -u admin -- bash -c '
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
python3 -c "
import dbus
bus = dbus.SessionBus()
p = bus.get_object(\"org.kde.StatusNotifierWatcher\",
                    \"/StatusNotifierWatcher\")
props = dbus.Interface(p, \"org.freedesktop.DBus.Properties\")
items = props.Get(\"org.kde.StatusNotifierWatcher\",
                  \"RegisteredStatusNotifierItems\")
for s in items:
    print(str(s))
" | tail -1
')
if echo "$ITEM_NAME" | grep -qE 'StatusNotifierItem-[0-9]+-1'; then
    echo "PASS: watcher has SNI item registered ($ITEM_NAME)"
else
    echo "FAIL: approval app did not register with watcher"
    echo "  items=$ITEM_NAME"
    cat /tmp/approval-app.log
    kill "$APP_PID" 2>/dev/null || true
    exit 3
fi

# 5. Read Title/Status from the SNI item itself. Initial state: 0
# pending → Title == "qdistro approvals" (no count suffix), Status
# == "Passive".
SNI_WKN=$(echo "$ITEM_NAME" | grep -oE 'org\.kde\.StatusNotifierItem-[0-9]+-1')
initial_title=$(runuser -u admin -- bash -c "
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
python3 -c '
import dbus
bus = dbus.SessionBus()
p = bus.get_object(\"$SNI_WKN\", \"/StatusNotifierItem\")
props = dbus.Interface(p, \"org.freedesktop.DBus.Properties\")
print(str(props.Get(\"org.kde.StatusNotifierItem\", \"Title\")))
'
")
initial_status=$(runuser -u admin -- bash -c "
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
python3 -c '
import dbus
bus = dbus.SessionBus()
p = bus.get_object(\"$SNI_WKN\", \"/StatusNotifierItem\")
props = dbus.Interface(p, \"org.freedesktop.DBus.Properties\")
print(str(props.Get(\"org.kde.StatusNotifierItem\", \"Status\")))
'
")
echo "initial: title=[$initial_title] status=[$initial_status]"
if [ "$initial_title" = "qdistro approvals" ] && [ "$initial_status" = "Passive" ]; then
    echo "PASS: initial SNI state is calm (Passive, no badge)"
else
    echo "FAIL: initial SNI state unexpected"
    kill "$APP_PID" 2>/dev/null || true
    exit 4
fi

# 6. Inject a RequestPermission → Title should gain "(1)" and Status
# should flip to Active (no urgent → not NeedsAttention).
runuser -u admin -- python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
iface.RequestPermission('qdistro.smoke.sni-badge',
                        dbus.Dictionary({'note': dbus.String('tray smoke')},
                                        signature='sv'))
print('requested', flush=True)
"
sleep 2

badge_title=$(runuser -u admin -- bash -c "
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
python3 -c '
import dbus
bus = dbus.SessionBus()
p = bus.get_object(\"$SNI_WKN\", \"/StatusNotifierItem\")
props = dbus.Interface(p, \"org.freedesktop.DBus.Properties\")
print(str(props.Get(\"org.kde.StatusNotifierItem\", \"Title\")))
'
")
badge_status=$(runuser -u admin -- bash -c "
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
python3 -c '
import dbus
bus = dbus.SessionBus()
p = bus.get_object(\"$SNI_WKN\", \"/StatusNotifierItem\")
props = dbus.Interface(p, \"org.freedesktop.DBus.Properties\")
print(str(props.Get(\"org.kde.StatusNotifierItem\", \"Status\")))
'
")
echo "after-inject: title=[$badge_title] status=[$badge_status]"
if echo "$badge_title" | grep -qE 'qdistro approvals \([0-9]+\)' && \
   [ "$badge_status" = "Active" ]; then
    echo "PASS: SNI title + status reflect pending count"
else
    echo "FAIL: SNI did not update after RequestPermission"
    kill "$APP_PID" 2>/dev/null || true
    exit 5
fi

# 7. ListHistory round-trip — new broker method. The deny we do here
# ensures the History tab can populate from real rows.
FIRST_ID=$(runuser -u admin -- python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
pending = iface.GetPending()
if pending: print(int(pending[0]['id']))
")
if [ -n "$FIRST_ID" ]; then
    runuser -u admin -- python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
iface.DecideRequest(dbus.Int32($FIRST_ID), 'deny', 'once')
"
fi

HISTORY_COUNT=$(runuser -u admin -- python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
print(len(iface.ListHistory(dbus.Int32(200))))
")
if [ "$HISTORY_COUNT" -ge 1 ]; then
    echo "PASS: ListHistory returned $HISTORY_COUNT row(s)"
else
    echo "FAIL: ListHistory returned 0 rows after an admin-decided request"
    kill "$APP_PID" 2>/dev/null || true
    exit 6
fi

# 8. Cleanup.
kill "$APP_PID" 2>/dev/null || true
kill "$(cat /tmp/watcher.pid)" 2>/dev/null || true
sleep 0.5

echo "PASS: §6.6 admin-approval-app SNI tray badge + ListHistory end-to-end"
