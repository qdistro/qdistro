# 10 — Qt admin app Cache tab: mouse-driven revoke + `ApprovalRevoked` signal

<!-- qci:visual: required -->

**Lane: qdwin** (native Wayland, the shipped launcher). Read the "qdwin lane"
section of `AGENTS.md` first: no xdotool, no `DISPLAY=:0`; graded frames come
from `qdwin_screenshot`.

**What**: seed the approval cache with four rows spanning different
scopes + uids, open the admin app's Cache tab, click a specific row
to select it, click the Revoke button, and verify that
(a) the row is gone from the table and from sqlite,
(b) the audit log gained one `source='revoke'` entry for it, and
(c) the broker broadcast exactly one `ApprovalRevoked` signal with the
right `(caller_uid, action, exe)` payload to a real D-Bus subscriber.

**Why**: the Cache tab is the admin's only GUI-driven way to unwind
a prior approval. This scenario exercises the full
admin→broker→sqlite+audit+signal path end-to-end through the GUI, and
demonstrates the mouse-intent pattern (AGENTS.md 3b) against a table row
instead of a simple radio button. permissions.md promises that revocation is
broadcast as a D-Bus signal so subscribers (qdshell first, others later) tear
down resources granted by the cached row at the same instant the row
disappears; (c) is that wire-level contract on the GUI revoke path. It used
to be scenario 22, which drove the same revoke by keyboard on the labwc lane.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui            # click-preview / click-confirm
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}

# Session up, idle locker held off and proven unlocked (no silo needed: the
# cache rows are seeded directly). A nonzero exit is a Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -f "[l]isten-broker-signal.py ApprovalRevoked" 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/10-signals.json /tmp/10-ready /tmp/10-sub.log /tmp/10-sub.pid'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'
SQL_APPR_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals;
SQL_EOF
)
SQL_AUDIT_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE source='revoke';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_APPR_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_AUDIT_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"

# Seed four approvals — distinct uids and scopes so the target row
# is unambiguous to pick visually. The target, curl.net, is a 24h row
# with no argv: the cache stores it as an exe_only row, so its
# match_value (the signal's exe argument) is /usr/bin/curl.
B64=$(base64 -w0 <<'EOF'
python3 - <<'PY'
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
c.store(2000, "test.action", "/usr/bin/python3.13", "1h", True, 1000)
c.store(2000, "curl.net", "/usr/bin/curl", "24h", True, 1000)
c.store(3000, "net.restart", "", "forever", True, 1000)
c.store(3000, "edit.hosts", "/usr/bin/vim", "forever_exe", True, 1000)
print("seeded 4 rows")
PY
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# Deliver the signal subscriber: a real dbus-python add_signal_receiver
# listener (the receive path qdshell uses), NOT dbus-monitor, whose
# BecomeMonitor eavesdrop has a start-up window and does not prove the
# ordinary `<allow receive_sender>` policy production subscribers rely on.
SUB_B64=$(base64 -w0 < "${QDISTRO_REPO}/tests/integration/permissions-gui/listen-broker-signal.py")
$VMEXEC "$VM" "echo $SUB_B64 | base64 -d > /tmp/listen-broker-signal.py"
```

## Steps

### S1 — launch admin app, switch to Cache tab, verify table

```bash
# The shipped launcher, first-paint mode (a nonzero exit FAILS S1).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_app'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'

# >>> Runner: take over here. Read the before-frame, locate the "Cache" tab
# header, preview / confirm a click on it (AGENTS.md 3b), then capture the
# Cache tab (it refreshes when switched to; the seeded rows land at once).
qdwin_screenshot "$ART/10-qt-cache-revoke-s1a-before.png"
# (runner clicks the "Cache" tab)
sleep 1
qdwin_screenshot "$ART/10-qt-cache-revoke-s1b-cache-tab.png"
```

**Assert (Cache tab active):**
- `s1b` shows the `Cache` tab header active (raised or highlighted) and a
 table with a header row (`id uid action scope exe expires`) plus four data
 rows.
- The four actions visible, in any order: `test.action`,
 `curl.net`, `net.restart`, `edit.hosts`.
- `Revoke` and `Refresh` buttons visible below the table.

### S2 — select the `curl.net` row, arm the subscriber, click Revoke

```bash
# >>> Runner: look at s1b, preview / confirm a click on the ROW whose action is
# `curl.net` (the uid=2000, scope=24h row; the action cell is most
# unambiguous), then capture:
sleep 1
qdwin_screenshot "$ART/10-qt-cache-revoke-s2a-row-selected.png"
```

Arm the signal subscriber now, just before the revoke, and BLOCK until it is
listening (its `--ready` file appears only after its match rule is
installed). Nothing before the Revoke click emits the signal, and starting a
process on the VM does not move the admin window's focus. `--timeout 300`
only bounds an orphan (the listener exits on its first signal); it must
outlast the gap from arming to your Revoke click, which includes a preview
round trip.

```bash
$VMEXEC "$VM" 'setsid python3 /tmp/listen-broker-signal.py ApprovalRevoked \
    --ready /tmp/10-ready --out /tmp/10-signals.json --timeout 300 \
    >/tmp/10-sub.log 2>&1 </dev/null &
  echo $! >/tmp/10-sub.pid'
$VMEXEC "$VM" 'for i in $(seq 1 50); do [ -f /tmp/10-ready ] && break; sleep 0.1; done; \
  [ -f /tmp/10-ready ] || { echo "subscriber NOT ready"; cat /tmp/10-sub.log; exit 1; }; \
  [ -s /tmp/10-signals.json ] && { echo "premature signal before revoke:"; cat /tmp/10-signals.json; exit 1; }; \
  echo "subscriber ready"'

# >>> Runner: preview / confirm a click on the "Revoke" button. Then wait
# for the broker's own record of the revoke before taking the frame: the
# signal file is written when the subscriber receives the signal.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_file /tmp/10-signals.json 15 && \
  for i in $(seq 1 30); do [ -s /tmp/10-signals.json ] && break; sleep 0.5; done; [ -s /tmp/10-signals.json ]'
sleep 1
qdwin_screenshot "$ART/10-qt-cache-revoke-s2b-after-revoke.png"
```

**Assert (after revoke):**
- The subscriber printed `subscriber ready` before the Revoke click, and
 `/tmp/10-signals.json` was still empty then (the readiness step exits 1
 otherwise, so this is executable, not observational).
- `s2a` shows the `curl.net` row highlighted (row-selected colour).
- `s2b` shows a table with exactly three rows remaining; no
 `curl.net` row. The remaining actions are `test.action`,
 `net.restart`, `edit.hosts` in some order.
- No error dialog appeared (Revoke succeeded silently).

### S3 — sqlite and audit recorded the revoke

```bash
B64=$(base64 -w0 <<'EOF'
echo "--- remaining cache ---"
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "SELECT action FROM approvals ORDER BY id"
echo "--- revoke audit rows ---"
sqlite3 /var/lib/qdistro/audit/audit.sqlite \
 "SELECT caller_uid, action, source, approver_uid FROM audit WHERE source='revoke' ORDER BY ts"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert:**
- Remaining cache rows: `test.action`, `net.restart`, `edit.hosts`.
 No `curl.net`.
- The revoke audit query prints exactly one row, `2000|curl.net|revoke|1000`:
 the admin app runs as `admin` (uid 1000), and the broker records the
 **calling process's uid** as the approver.

### S4 — exactly one signal, correct payload

```bash
$VMEXEC "$VM" 'cat /tmp/10-signals.json 2>/dev/null; echo; cat /tmp/10-sub.log'
```

**Assert** (`/tmp/10-signals.json` is JSON-lines — one JSON object per
captured signal, as written by `listen-broker-signal.py`):
- The file contains exactly **one** line (one signal fired).
- That line is an object whose `args` array is exactly, in order:
  `2000` (caller_uid), `"curl.net"` (action), `"/usr/bin/curl"` (the row's
  match_value), i.e. the file reads:
  `{"member": "ApprovalRevoked", "args": [2000, "curl.net", "/usr/bin/curl"]}`

## Teardown

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" '[ -f /tmp/10-sub.pid ] && kill "$(cat /tmp/10-sub.pid)" 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/10-signals.json /tmp/10-ready /tmp/10-sub.log /tmp/10-sub.pid'
SQL_APPR_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals;
SQL_EOF
)
SQL_AUDIT_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE source='revoke';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_APPR_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_AUDIT_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- The Cache tab triggers a refresh every time it's switched to —
 so any seeded rows land as soon as you click the tab; no explicit
 Refresh click is needed in S1.
- The table supports single-row selection; a second click on a
 different row replaces the selection. If you clicked the wrong row,
 click the right one before hitting Revoke.
- `approver_uid=1000` in the audit row reflects the admin app
 running as `admin`. The broker records whichever uid made the
 D-Bus call, not whatever uid kicked off the launcher chain.
- The signal's `exe` argument carries the cache row's `match_value`
 (the exe captured at decide-time), NOT a re-derived live caller exe.
 For an exe_only row (a 1h/24h row without argv, or forever_exe) that is
 the exe; for a `forever` row it is empty. The subscriber is proven
 listening before the click, so a revoked row with no signal, or a signal
 with the wrong payload, is a FAIL. If the subscriber itself died first
 (`/tmp/10-sub.log` shows its error, or it timed out because the click came
 more than 300 s after arming), that is ERROR.
