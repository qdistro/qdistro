#!/bin/bash
# Step D acceptance: as user 'work', request a permission; verify it
# shows up pending in the broker; then this script returns and the
# driver (host) sends the Approve keystroke.

set -u

echo "[step-d] starting test-permission.py as 'work' in background"
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission > /tmp/test-output.txt 2>&1 & echo $! > /tmp/test-pid'

sleep 2

echo "[step-d] broker pending requests:"
python3 <<'PY'
import dbus
bus = dbus.SystemBus()
obj = bus.get_object("com.qdistro.AdminBroker1", "/com/qdistro/AdminBroker1")
for r in obj.GetPending(dbus_interface="com.qdistro.AdminBroker1"):
    print(dict(r))
PY

echo "[step-d] test-pid: $(cat /tmp/test-pid)"
echo "[step-d] waiting for approval via admin app..."
