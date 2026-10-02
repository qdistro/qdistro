# 34 — Admin app navigation across multiple pending requests

<!-- qci:visual: required -->

**Lane: qdwin** (native Wayland, the shipped launcher). Read the "qdwin lane"
section of `AGENTS.md` first: no xdotool, no `DISPLAY=:0`; graded frames come
from `qdwin_screenshot`.

**What**: inject three pending requests at once (from three
caller PIDs as `work`), launch the Qt admin app. Verify all three
rows appear in the Pending list, arrow-Down moves selection from
row 0 → row 1 → row 2, the detail pane updates each time with the
matching caller info, and approving row 1 leaves rows 0 and 2 with
their original selection-targets preserved.

**Why**: scenario 03 covers single-row rendering; 04 covers a
single approve. Real admin sessions have queues. The selection
state machine — keep the previously-selected request highlighted
across refreshes when possible, fall back to row 0 otherwise — is
non-trivial and easy to regress (see `MainWindow.refresh`). A regression that reset selection to row 0 on every
signal would feel broken to admin in a busy queue.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}

# Session up, work/work2 silo fixtures (the callers run as work), idle locker
# held off and proven unlocked. A nonzero exit is a Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup --silos'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'

APPROVALS_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action LIKE 'multi.%';
SQL_EOF
)
AUDIT_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action LIKE 'multi.%';
SQL_EOF
)
$VMEXEC "$VM" "echo $APPROVALS_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $AUDIT_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Steps

### S1 — launch admin app on empty queue

```bash
# The shipped launcher, first-paint mode (a nonzero exit FAILS S1).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_app'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'
qdwin_screenshot "$ART/34-s1-empty.png"
```

**Assert**: pending list empty; the window is fully drawn (a partly drawn
window is a FAIL on this lane).

### S2 — inject three pending requests

```bash
B64=$(base64 -w0 <<'EOF'
# Three callers with distinct actions so they are visually distinct.
# qdistro-test-permission accepts --action and --detail since
# todo/qdistro-test-permission-multi-action.md landed.
source /tmp/qci-gui-waiters.sh
for i in 1 2 3; do
  bg_start "34-w$i" work "python3 /usr/local/bin/qdistro-test-permission \
      --action multi.action.$i --detail slot=$i"
done
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# All three are displayed (the title counts the rows the list shows). A
# timeout FAILS S2.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals \(3 pending\)" 30'
qdwin_screenshot "$ART/34-s2-three-rows.png"
$VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.GetPending'
```

**Assert**:
- `34-s2-three-rows.png` shows exactly three rows in the
  Pending list, in some order: `multi.action.1`, `multi.action.2`,
  `multi.action.3` (all under `uid=2000`).
- Detail pane shows row 0's action — whichever of the three is
  topmost.
- `GetPending` reports three structs.

### S3 — arrow-Down twice, detail pane tracks the selection

Keyboard focus is on the Pending list after launch (the app focuses it
whenever the Pending tab is current); `qdwin_focus_window` makes sure the
compositor routes the KVM keys to this window. A selection move publishes no
state a waiter can read, so each frame follows a 1 s settle.

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'

virsh -c qemu:///session send-key "$VM" --codeset linux KEY_DOWN
sleep 1
qdwin_screenshot "$ART/34-s3a-second-selected.png"

virsh -c qemu:///session send-key "$VM" --codeset linux KEY_DOWN
sleep 1
qdwin_screenshot "$ART/34-s3b-third-selected.png"
```

**Assert** (vision, both frames):
- `s3a`: detail pane's Action line reads the action of the SECOND
  visible row. (The visible-row order is whatever GetPending
  returned; verify the detail pane matches whichever row is
  highlighted with the selection-color background.)
- `s3b`: detail pane's Action line reads the action of the THIRD
  visible row. The previously-selected row is no longer
  highlighted.

### S4 — approve the third row; survivors stay

```bash
# Default scope is "once" -> no cache row, just a decide. Use the broker
# API for the decision so this scenario remains focused on the admin app's
# multi-row navigation and refresh behavior rather than Qt shortcut delivery.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- python3 - <<'PYEOF'
import dbus

bus = dbus.SystemBus()
obj = bus.get_object("org.qdistro.AdminBroker1",
                     "/org/qdistro/AdminBroker1")
iface = dbus.Interface(obj, "org.qdistro.AdminBroker1")
rows = [r for r in iface.GetPending()
        if str(r.get("action", "")).startswith("multi.action.")]
if len(rows) != 3:
    raise SystemExit(f"expected 3 multi.action rows, got {len(rows)}")
iface.DecideRequest(int(rows[2]["id"]), "allow", "once")
PYEOF
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# The title is computed from the Pending model's row count, and an external
# decision reaches the app only through the broker's signal -> refresh, so
# waiting for the exact title proves the app itself reacted; qdwin logs it on
# the commit that carries that frame. A timeout FAILS S4.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals \(2 pending\)" 30'
qdwin_screenshot "$ART/34-s4-after-approve.png"

$VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.GetPending'
```

**Assert**:
- `34-s4-after-approve.png` shows exactly two rows in the
  Pending list. The action approved in S4 is gone; the other two
  remain.
- `GetPending` returns two structs whose actions are the un-
  approved subset of `{multi.action.1, multi.action.2,
  multi.action.3}`.
- Detail pane is NOT `(no selection)` — the broker app's
  refresh logic should preserve a sensible target row (row 0
  after the deleted row is gone) and the detail pane should
  display its action.

### S5 — clean up the surviving two requests by denying both

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- python3 - <<'PYEOF'
import dbus

bus = dbus.SystemBus()
obj = bus.get_object("org.qdistro.AdminBroker1",
                     "/org/qdistro/AdminBroker1")
iface = dbus.Interface(obj, "org.qdistro.AdminBroker1")
for row in list(iface.GetPending()):
    if str(row.get("action", "")).startswith("multi.action."):
        iface.DecideRequest(int(row["id"]), "deny", "once")
PYEOF
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# A timeout FAILS S5.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'
qdwin_screenshot "$ART/34-s5-drained.png"

# Every request is decided now (S4 approved one, S5 denied the other two),
# so all three callers must finish. `wait $(cat X.pid)` here never waited
# (the pid is not this shell's child), so this drain was a no-op and the
# teardown pkill did the real work. bg_wait only says a caller FINISHED;
# the caller's own exit status is the decision it received: 0 = ALLOWED,
# 1 = DENIED. Two denied callers exiting 1 is the expected outcome, not a
# failure. The drain checks the multiset: exactly one 0 and two 1s.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh
bad=0 rcs=
for i in 1 2 3; do
  bg_wait "34-w$i" 60 || { echo "34-w$i never finished" >&2; bad=1; }
  rc=$(bg_rc "34-w$i") || rc=none
  echo "34-w$i rc=$rc"
  rcs="$rcs$rc "
done
[ "$(printf "%s\n" $rcs | sort | tr "\n" " ")" = "0 1 1 " ] ||
  { echo "caller exit codes are [$rcs], wanted one 0 (approved) and two 1 (denied)" >&2; bad=1; }
exit $bad'
```

**Assert** (drain): the command exits 0. Each caller prints `rc=0`
(`ALLOWED`) or `rc=1` (`DENIED`), and the set is exactly one `0` and two
`1`s. Do not FAIL the scenario because the denied callers exited 1. That
is the decision they were sent. FAIL only when a caller never finished or
the set of exit codes is different.

**Assert**: `34-s5-drained.png` shows an empty pending list and
`(no selection)` in the detail pane. On this lane the frame after the title
wait is the app's committed state, so a frame that still shows a row is a
FAIL, not a reason to recapture.

## Teardown

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/34-w*.pid'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action LIKE 'multi.%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- The `multi.action.{1,2,3}` action namespace is chosen so the
  rows are visually distinguishable in the Pending list AND in
  the detail pane — every row has a different `Action:` string.
- Pending-list row order is NOT guaranteed by the broker — it's
  insertion order, but the python sub-shells in S2 race. Don't
  assert "row 0 is action.1" — assert the *set* of three actions
  is the right set, and that arrow-Down moves selection by one
  row in the rendered order.
- If S4's "two rows remain" check fails with three rows, S4's
  approve never took effect in the app. S3 only navigates (no
  decision is sent there). Find which of three steps failed, in
  order: the admin API call was refused (typically `python3`
  run without the `-`, see the next note), `DecideRequest`
  itself raised, or the decision landed but the app never
  refreshed (`GetPending` shows two rows while the title wait
  timed out with the title still `(3 pending)`).
- Run the S4/S5 decision scripts exactly as written, as
  `runuser -u admin -- python3 -`. The `-` is required: the broker
  trusts an admin Python peer that reads its script from stdin only
  when argv says so (`-` or `-c`), and refuses a bare `python3` with
  "Python peer is not an installed admin script" (2026-09-24: a
  driver dropped the `-`, GetPending was refused, S4 never decided).
- If a decision script or a title wait fails, that step has
  failed: record it with the command's stderr, and do not release
  the next guest gate before you have. Releasing the guest early let
  its S5 deny-all drain every row before the S4 retry (2026-09-24).
- This scenario used to run on the labwc/XWayland lane, where frames
  routinely lagged the title (a "recapture up to 4 times" allowance and a
  history of black/clipped rows). On the qdwin lane a frame that disagrees
  with the title is a real FAIL; report it with the frame.
