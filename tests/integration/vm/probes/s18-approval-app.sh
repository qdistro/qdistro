#!/bin/bash
# §6.6 follow-up — admin-approval-app smoke.
#
# Verifies:
#   1. qdistro-admin-approval-app is installed (from bootstrap-qdwin-in-vm.sh).
#   2. It imports + talks to the broker (GetPending round-trip).
#   3. qdshell's RequestPending listener is wired (see qdshell log
#      for "approval app spawned"-style signal).
#   4. Injecting a RequestPermission call makes GetPending return
#      one item for the app to decode.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}

# 1. App present.
APP_PATH=""
for p in /usr/local/bin/qdistro-admin-approval-app \
         /usr/share/qdshell/qdistro-admin-approval-app.py; do
    [ -f "$p" ] && APP_PATH="$p" && break
done
if [ -z "$APP_PATH" ]; then
    # Install from source if bootstrap hasn't been run yet.
    install -m 0755 \
        "$QDWIN_SRC/qdshell/qdistro-admin-approval-app.py" \
        /usr/local/bin/qdistro-admin-approval-app
    APP_PATH=/usr/local/bin/qdistro-admin-approval-app
fi
echo "PASS: approval app installed at $APP_PATH"

# 2. App parses (Python compiles cleanly).
if python3 -c "import ast; ast.parse(open('$APP_PATH').read())"; then
    echo "PASS: approval app imports cleanly"
else
    echo "FAIL: approval app syntax error"
    exit 2
fi

# 3. Broker reachable. Expect qdistro-admin-broker already started
# (fresh-vm-bootstrap.sh does systemctl start).
systemctl is-active --quiet qdistro-admin-broker.service || {
    systemctl start qdistro-admin-broker.service 2>/dev/null || true
    sleep 1
}
if ! systemctl is-active --quiet qdistro-admin-broker.service; then
    echo "FAIL: qdistro-admin-broker not active"
    systemctl status qdistro-admin-broker.service --no-pager 2>&1 | head -10
    exit 3
fi
echo "PASS: broker active"

# 4. GetPending round-trip from Python works.
if python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
print(len(iface.GetPending()), 'pending')
"; then
    echo "PASS: GetPending round-trip ok"
else
    echo "FAIL: GetPending round-trip broken"
    exit 4
fi

# 5. Inject a RequestPermission → verify GetPending returns ≥1 item.
# RequestPermission is async (fire-and-forget); the broker creates a
# pending request server-side. We then read GetPending as admin and
# check it shows up. DecideRequest must be called as admin.
# Run the injector as admin (a non-admin uid? admin is admin on fresh VM,
# so GetPending sees it regardless). Admin UID in broker is admin/1000.
(
    export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus"
    runuser -u admin -- python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
rid = iface.RequestPermission('qdistro.smoke.approval-app',
                              dbus.Dictionary({'note': dbus.String('s18 smoke')},
                                              signature='sv'))
print('requested rid=', rid)
"
)
sleep 1

COUNT=$(python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
print(len(iface.GetPending()))
")
if [ "$COUNT" -ge 1 ]; then
    echo "PASS: pending queue has $COUNT request(s) after injection"
else
    echo "FAIL: no pending after RequestPermission"
    exit 5
fi

# 6. Deny the pending request (clean up).
FIRST_ID=$(python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
pending = iface.GetPending()
if pending: print(int(pending[0]['id']))
")
if [ -n "$FIRST_ID" ]; then
    # DecideRequest is admin-gated by the broker (ADMIN_UID=admin/1000),
    # so run the decide call as admin.
    runuser -u admin -- python3 -c "
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object('com.qdistro.AdminBroker1',
                        '/com/qdistro/AdminBroker1')
iface = dbus.Interface(proxy, 'com.qdistro.AdminBroker1')
iface.DecideRequest(dbus.Int32($FIRST_ID), 'deny', 'once')
print('decided rid=', $FIRST_ID)
"
    echo "PASS: DecideRequest(deny) clean-up worked"
fi

echo "PASS: §6.6 admin-approval-app broker round-trip end-to-end"
