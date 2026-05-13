# 08 — Admin app signal subscription survives broker restart

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
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1
```

## Steps

### S1 — launch admin app on a clean broker, verify empty state

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
$VMGUI "$VM" screenshot /tmp/08-s1-empty.png
```

**Assert:**
- Window `admin approvals` is visible.
- Left list is empty; detail pane reads `(no selection)`.

### S2 — restart the broker while the admin app stays up

```bash
# Note the broker's current PID before, and after — must differ.
$VMEXEC "$VM" 'pgrep -f qdistro_admin_broker.py | head -1 > /tmp/08-pid-before'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 2
$VMEXEC "$VM" 'pgrep -f qdistro_admin_broker.py | head -1 > /tmp/08-pid-after'
# Print both pids as separate lines — "before=...\nafter=..." would
# need embedded double quotes which vm-exec's JSON encoder doesn't
# handle (AGENTS.md ). One cat per file keeps the payload quote-free.
$VMEXEC "$VM" 'echo before=$(cat /tmp/08-pid-before); echo after=$(cat /tmp/08-pid-after)'
# Admin app should still be alive.
$VMEXEC "$VM" 'pgrep -u admin -f qdistro_admin_app.py | head -1'
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
#!/bin/bash
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
 >/tmp/08-work.log 2>&1 & echo $! >/tmp/08-work.pid'
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
$VMEXEC "$VM" 'wait $(cat /tmp/08-work.pid) 2>/dev/null; cat /tmp/08-work.log'
```

**Assert:**
- Screenshot shows empty list + detail pane `(no selection)`.
- `/tmp/08-work.log` contains `DENIED` — the SDK actually saw the
 deny (not just the UI).

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/08-pid-before /tmp/08-pid-after /tmp/08-work.log /tmp/08-work.pid'
```

## Notes for the runner

- Do NOT kill/relaunch the admin app between S1 and S3; the whole
 point is verifying the _long-running_ app handles a broker
 restart. Teardown at the end is fine.
- If S3 sees an empty list, also check `pgrep qdistro_admin_app`
 to rule out the app having crashed — if it crashed the bug is
 different (not the signal-filter regression the scenario
 targets).
