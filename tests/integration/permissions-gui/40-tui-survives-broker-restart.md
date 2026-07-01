# 40 — TUI survives broker restart (signal subscription resilience)

**What**: launch the TUI (`qdistro_admin_tui.py`), restart
`qdistro-admin-broker.service` mid-session, inject a pending
permission request via `busctl call`, and verify the TUI's pending
pane updates without manual refresh or relaunch.

**Why**: dbus-python's `add_signal_receiver(... bus_name=...)` resolves
the well-known name to a unique sender name once; if the broker
restarts, the filter silently stops delivering. The TUI's
`DBusBrokerClient` already uses the `bus_name`-free pattern (no
well-known-name filter on signal subscription) plus a `_reconnect()`
fallback, so this is a **coverage scenario** confirming existing
correctness — not fixing a bug. Scenario 08 validates the same
property for the Qt admin app; this is the TUI counterpart.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
# Clear any cached approvals / rules for test.action so S3 always
# produces a fresh pending row. Without this, a leftover "forever"
# rule would short-circuit the prompt and S3 would never see the row.
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/*test.action* 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /var/lib/qdistro/cache/*.db 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1
```

## Steps

### S1 — launch TUI on a clean broker, verify empty state

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-tui'
sleep 3
$VMGUI "$VM" screenshot /tmp/40-s1-empty.png
```

**Assert:**
- Header subtitle reads `(no pending requests) • scope: Just this once`.
- Right pane shows `(no request selected)`.
- No `BROKER OFFLINE` banner.

### S2 — restart the broker while the TUI stays up

```bash
# Note the broker's current PID before, and after — must differ.
$VMEXEC "$VM" 'pgrep -f "[q]distro_admin_broker.py" | head -1 > /tmp/40-pid-before'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 2
$VMEXEC "$VM" 'pgrep -f "[q]distro_admin_broker.py" | head -1 > /tmp/40-pid-after'
$VMEXEC "$VM" 'echo before=$(cat /tmp/40-pid-before); echo after=$(cat /tmp/40-pid-after)'
# TUI should still be alive.
$VMEXEC "$VM" 'pgrep -u admin -f "[q]distro_admin_tui.py" | head -1'
```

**Assert:**
- Broker PID before != PID after (service actually restarted).
- TUI process still running (restart didn't take it down).

### S3 — inject a pending request; TUI must see it

This is the crux: a well-known-name-filtered signal subscription
would silently drop the new broker's `RequestPending`, and the TUI
would stay empty despite sqlite showing a pending row.

```bash
B64=$(base64 -w0 <<'EOF'
#!/bin/bash
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
 >/tmp/40-work.log 2>&1 & echo $! >/tmp/40-work.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# Give the TUI at least a debounce cycle (250ms) plus D-Bus signal
# delivery; 2s is plenty.
sleep 2
$VMGUI "$VM" screenshot /tmp/40-s3-pending.png
```

**Assert:**
- Screenshot shows one pending row `uid=2000 test.action` in the
  left-side DataTable.
- Right pane shows request details: `uid=2000 pid=<N>`,
  `Action: test.action`, executable path, details.
- If this fails — empty table despite the broker holding the
  request — the signal subscription has regressed to the
  well-known-name-filtered pattern.

### S4 — deny the request, confirm return to empty

```bash
# The TUI runs inside a terminal (qterminal or bare TTY). Focus it
# and press ctrl+n (deny).
B64=$(base64 -w0 <<'EOF'
#!/bin/bash
runuser -u admin -- env DISPLAY=:0 \
 xdotool search --sync --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 1
$VMGUI "$VM" screenshot /tmp/40-s4-afterdeny.png
$VMEXEC "$VM" 'wait $(cat /tmp/40-work.pid) 2>/dev/null; cat /tmp/40-work.log'
```

**Assert:**
- Screenshot shows empty DataTable + right pane `(no request selected)`.
- `/tmp/40-work.log` contains `DENIED` — the SDK actually saw the
  deny (not just the UI).

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/40-pid-before /tmp/40-pid-after /tmp/40-work.log /tmp/40-work.pid'
```

## Notes for the runner

- Do NOT kill/relaunch the TUI between S1 and S3; the whole point is
  verifying the _long-running_ TUI handles a broker restart.
  Teardown at the end is fine.
- If S3 sees an empty table, also check `pgrep qdistro_admin_tui`
  to rule out the TUI having crashed — if it crashed the bug is
  different (not the signal-subscription regression the scenario
  targets).
- The TUI's `DBusBrokerClient._reconnect()` rebuilds the proxy on
  the first D-Bus call after the broker restarts. The signal
  subscription uses the `bus_name`-free pattern so new-owner signals
  arrive without re-subscribing. Between S2 and S3, the TUI's
  safety poll (every 30s) may also trigger a `_reconnect`. The 2s
  sleep in S3 is enough because the `RequestPending` signal fires
  from the new broker immediately; if it doesn't arrive, the
  subscription pattern has regressed.
- This scenario is a coverage test — the code is already correct.
  Scenario 08 covers the same property for the Qt admin app; this
  file covers the TUI counterpart.
