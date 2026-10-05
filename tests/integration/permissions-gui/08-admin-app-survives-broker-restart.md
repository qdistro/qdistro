# 08 — Admin app signal subscription survives broker restart

<!-- qci:visual: required -->

**Lane: qdwin** (native Wayland, the shipped launcher). Read the "qdwin lane"
section of `AGENTS.md` first: no xdotool, no `DISPLAY=:0`; graded frames come
from `qdwin_screenshot`.

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
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}

# Session up, work/work2 silo fixtures, idle locker held off and proven
# unlocked. A nonzero exit is a Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup --silos'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
# Pending requests live in the broker's in-memory queue, not sqlite.
# Restart empties it. Prove GetPending is empty before launching the
# app — a leftover `uid=2000 test.action` row falsifies S1
# (full-20260918T143937Z-3516587).
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service && await_dbus_system_name org.qdistro.AdminBroker1'
# Count via the Python API as admin stdin (`python3 -`). Root python3
# is AccessDenied (gui-admin-20260919T114809Z-1540720); root dbus-send
# is trusted, but its text is the wrong oracle, and `grep -q && exit 1`
# fails the empty (success) case.
B64=$(base64 -w0 <<'PYEOF'
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
)
$VMEXEC "$VM" "echo $B64 | base64 -d | runuser -u admin -- python3 -"
```

## Steps

### S1 — launch admin app on a clean broker, verify empty state

```bash
# The shipped launcher, first-paint mode (a nonzero exit FAILS S1).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_app'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'
$VMEXEC "$VM" 'dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.GetPending'
qdwin_screenshot "$ART/08-s1-empty.png"
```

**Assert:**
- Setup printed `pending_count=0`. This is the model behind the
  pane; a leftover request here is a setup failure, not a
  signal-subscription regression.
- The admin approvals window is visible (the title wait proved its title).
- Left list is empty; detail pane reads `(no selection)`.

### S2 — restart the broker while the admin app stays up

```bash
# Note the broker's current PID before, and after — must differ.
$VMEXEC "$VM" 'pgrep -f "[q]distro_admin_broker.py" | head -1 > /tmp/08-pid-before'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'
$VMEXEC "$VM" 'pgrep -f "[q]distro_admin_broker.py" | head -1 > /tmp/08-pid-after'
# Print both pids as separate lines — "before=...\nafter=..." would
# need embedded double quotes which vm-exec's JSON encoder doesn't
# handle (AGENTS.md ). One cat per file keeps the payload quote-free.
$VMEXEC "$VM" 'echo before=$(cat /tmp/08-pid-before); echo after=$(cat /tmp/08-pid-after)'
# Admin app should still be alive (the shipped app path).
$VMEXEC "$VM" 'pgrep -u admin -f "[q]distro-admin-approval-app" | head -1'
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
# The request is in the broker; the APP shows it only if its signal
# subscription survived the restart. Its title counts the rows its Pending
# list displays, so this wait IS the crux: a timeout FAILS S3 (the app went
# blind), whatever the frame shows.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_broker_pending_action test.action 30'
s3_rc=0
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals \(1 pending\)" 30' || s3_rc=$?
echo "s3 title-wait rc=$s3_rc"
qdwin_screenshot "$ART/08-s3-pending.png"
```

**Assert:**
- `s3 title-wait rc=0`.
- Screenshot shows one pending row `uid=2000 test.action` in the
 left list, row selected (highlighted).
- Detail pane shows `uid=2000 pid=<N>`, `Action: test.action`,
 `/usr/bin/python3.14`, `Details: purpose=smoke test`.
- If this fails — empty list despite the broker holding the
 request — the signal-subscription fix has regressed.

### S4 — deny the request, confirm return to empty

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
virsh -c qemu:///session send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
# The list is empty again (title back to bare) before the frame is taken.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'
qdwin_screenshot "$ART/08-s4-afterdeny.png"
# bg_wait, never `wait $(cat X.pid)` — that does not wait in a separate guest
# shell (AGENTS.md, "A backgrounded job"). A TIMEOUT here IS this step's failure.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 08-work 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 08-work; echo "rc=$(bg_rc 08-work)"'
# The SDK-side oracle, read through bg_log (the only reader of a bg job's
# output). Do NOT grep a log by path: the job's files live in $QCI_BG_DIR,
# not the per-scenario scratch dir, and a guessed path reads as a missing
# DENIED (full-20260930T051422Z-65193 ERROR'd that way with DENIED logged).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; if ! out=$(bg_log 08-work); then echo WORK_SAW_DENIED=unreadable; elif printf "%s\n" "$out" | grep -x DENIED >/dev/null; then echo WORK_SAW_DENIED=yes; else echo WORK_SAW_DENIED=no; fi'
```

**Assert:**
- Screenshot shows empty list + detail pane `(no selection)`.
- The last command prints `WORK_SAW_DENIED=yes` — the SDK actually saw
 the deny (not just the UI). `WORK_SAW_DENIED=no` (the log was read and
 has no `DENIED` line) is a product FAIL; `WORK_SAW_DENIED=unreadable`
 (bg_log could not read the job's log) is a harness ERROR.

## Teardown

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
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
- If S3 sees an empty list, also check `pgrep -u admin -f qdistro-admin-approval-app`
 to rule out the app having crashed — if it crashed the bug is
 different (not the signal-filter regression the scenario
 targets).
