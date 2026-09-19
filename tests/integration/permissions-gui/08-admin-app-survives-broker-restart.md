# 08 — Admin app signal subscription survives broker restart

<!-- qci:visual: required -->

**What**: start the Qt admin app, restart `qdistro-admin-broker.service`
mid-session, inject a permission request from `work`, verify the
admin app shows the new pending row **without** being manually
restarted or refreshed.

**Why**: dbus-python's `add_signal_receiver(... bus_name=...)` resolves
the well-known name to a unique sender name once; when the broker
restarts the filter silently stops delivering. Commit `d72a430`
dropped that filter so fresh-broker signals still arrive. This
scenario is the operational acceptance of that fix — without it,
the failure is invisible (admin quietly goes blind to new requests).

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
# Pending requests live in the broker's in-memory queue, not sqlite.
# Restart empties it. Prove GetPending is empty before launching the
# app — a leftover `uid=2000 test.action` row falsifies S1
# (full-20260918T143937Z-3516587).
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service && await_dbus_system_name org.qdistro.AdminBroker1'
# Count via the Python API. dbus-send text is the wrong oracle, and
# `grep -q && exit 1` fails the empty (success) case.
B64=$(base64 -w0 <<'EOF'
python3 - <<'PYEOF'
import dbus, sys
bus = dbus.SystemBus()
obj = bus.get_object("org.qdistro.AdminBroker1",
                     "/org/qdistro/AdminBroker1")
iface = dbus.Interface(obj, "org.qdistro.AdminBroker1")
n = len(iface.GetPending())
print(f"pending_count={n}")
if n != 0:
    print("FAIL(setup): GetPending not empty after broker restart",
          file=sys.stderr)
    sys.exit(1)
PYEOF
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Steps

### S1 — launch admin app on a clean broker, verify empty state

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
$VMEXEC "$VM" 'dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.GetPending'
$VMGUI "$VM" screenshot /tmp/08-s1-empty.png
```

**Assert:**
- Setup printed `pending_count=0`. This is the model behind the
  pane; a leftover request here is a setup failure, not a
  signal-subscription regression.
- Window `admin approvals` is visible.
- Left list is empty; detail pane reads `(no selection)`.

### S2 — restart the broker while the admin app stays up

```bash
# Note the broker's current PID before, and after — must differ.
$VMEXEC "$VM" 'pgrep -f "[q]distro_admin_broker.py" | head -1 > /tmp/08-pid-before'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 2
$VMEXEC "$VM" 'pgrep -f "[q]distro_admin_broker.py" | head -1 > /tmp/08-pid-after'
# Print both pids as separate lines — "before=...\nafter=..." would
# need embedded double quotes which vm-exec's JSON encoder doesn't
# handle (AGENTS.md ). One cat per file keeps the payload quote-free.
$VMEXEC "$VM" 'echo before=$(cat /tmp/08-pid-before); echo after=$(cat /tmp/08-pid-after)'
# Admin app should still be alive.
$VMEXEC "$VM" 'pgrep -u admin -f "[q]distro_admin_app.py" | head -1'
```

**Assert:**
- Broker PID before != PID after (service actually restarted).
- Admin app process still running (restart didn't take it down).

### S3 — trigger a new work request; admin app must see it

This is the crux: a well-known-name-filtered signal subscription
would silently drop the new broker's `RequestPending`, and the UI
would stay empty despite sqlite showing a pending row.

```bash
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start 08-work work 'python3 /usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# Give the admin app at least a debounce cycle (250ms) plus DBus
# signal delivery; 2s is plenty.
sleep 2
$VMGUI "$VM" screenshot /tmp/08-s3-pending.png
```

**Assert:**
- Screenshot shows one pending row `uid=2000 test.action` in the
 left list, row selected (highlighted).
- Detail pane shows `uid=2000 pid=<N>`, `Action: test.action`,
 `/usr/bin/python3.13`, `Details: purpose=smoke test`.
- If this fails — empty list despite the broker holding the
 request — the signal-subscription fix has regressed.

### S4 — deny the request, confirm return to empty

```bash
B64=$(base64 -w0 <<'EOF'
#!/bin/bash
runuser -u admin -- env DISPLAY=:0 \
 xdotool search --sync --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 1
$VMGUI "$VM" screenshot /tmp/08-s4-afterdeny.png
# bg_wait, never `wait $(cat X.pid)` — that does not wait in a separate guest
# shell (AGENTS.md, "A backgrounded job"). A TIMEOUT here IS this step's failure.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 08-work 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 08-work; echo "rc=$(bg_rc 08-work)"'
```

**Assert:**
- Screenshot shows empty list + detail pane `(no selection)`.
- `/tmp/08-work.log` contains `DENIED` — the SDK actually saw the
 deny (not just the UI).

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/08-pid-before /tmp/08-pid-after /tmp/08-work.log /tmp/08-work.pid /tmp/08-work.rc /tmp/08-work.rc.part'
```

## Notes for the runner

- Do NOT kill/relaunch the admin app between S1 and S3; the whole
 point is verifying the _long-running_ app handles a broker
 restart. Teardown at the end is fine.
- Do not start `qdistro-test-permission` (or any work request) until
  S3. S1 asserts an empty pane; injecting the request early is a
  setup failure, not a product FAIL.
- If S3 sees an empty list, also check `pgrep qdistro_admin_app`
 to rule out the app having crashed — if it crashed the bug is
 different (not the signal-filter regression the scenario
 targets).
