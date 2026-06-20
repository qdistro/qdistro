# 22 — `ApprovalRevoked` signal payload, GUI revoke path

**What**: with one cached approval seeded for `work` (uid 2000),
attach a real D-Bus signal subscriber, then revoke the row from
the Qt admin app's Cache tab. Verify (a) one `ApprovalRevoked`
signal is emitted with the correct `(caller_uid, action, exe)`
payload, (b) the cache row is gone, (c) an audit row with
`source='revoke'` was written.

**Why**: permissions.md  promises that revocation is broadcast as
a D-Bus signal so subscribers (qdshell first, others later) tear down
resources granted by the cached row at the same instant the row
disappears. Scenario 10 covers the GUI mechanics of revoke; this
scenario is the wire-level contract — payload shape and one-signal-
per-row.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -f "[d]bus-monitor.*ApprovalRevoked" 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

APPROVALS_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals;
SQL_EOF
)
AUDIT_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE source='revoke';
SQL_EOF
)
$VMEXEC "$VM" "echo $APPROVALS_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $AUDIT_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"

# Seed one approval — forever_exe so match_value carries an exe path
# the signal payload can be checked against.
B64=$(base64 -w0 <<'EOF'
python3 - <<'PY'
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
c.store(2000, "test.action", "/usr/bin/python3.13", "forever_exe", True, 1000)
print("seeded 1 row")
PY
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# Deliver the signal subscriber. We use a real dbus-python
# add_signal_receiver listener (the same receive path qdshell uses),
# NOT `dbus-monitor`: dbus-monitor's BecomeMonitor eavesdrop has a
# start-up window where a revoke fired too early is missed, and it does
# not prove the ordinary `<allow receive_sender>` policy that production
# subscribers depend on. The helper touches a readiness file only AFTER
# its match rule is installed, closing the subscribe/emit race.
SUB_B64=$(base64 -w0 < "${QDISTRO_REPO}/tests/integration/permissions-gui/listen-broker-signal.py")
$VMEXEC "$VM" "echo $SUB_B64 | base64 -d > /tmp/listen-broker-signal.py"
```

## Steps

### S1 — launch the admin app

The signal subscriber is deliberately NOT started here. It is armed in S2
immediately before the revoke keypress, so its `--timeout` budget covers only
the Space→emit latency rather than the whole GUI launch/nav/screenshot
choreography below (see the comment in S2 for why this matters).

```bash
# Reap any listener left over from an interrupted prior run, then clear its
# files so the readiness/premature-signal checks in S2 start from clean state.
$VMEXEC "$VM" 'pkill -f "[l]isten-broker-signal.py ApprovalRevoked" 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/22-signals.json /tmp/22-ready /tmp/22-sub.log /tmp/22-monitor.pid'

$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3

B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/22-s1-launched.png
```

**Assert**:
- Window `admin approvals` is visible.

### S2 — keyboard: reach Cache tab, select row, press Revoke

Mouse clicks are platform-blocked on the XWayland GUI template
(AGENTS.md §3b), so drive the whole revoke path with the keyboard via
`virsh send-key` (the blessed input path, §3a). After launch the
window focuses the Pending list; one **Shift+Tab** moves focus back to
the tab bar, **Right** switches Pending→Cache, **Tab** enters the
Cache table, **Down** selects the `test.action` row (sets the table's
`currentIndex`, which `btn_revoke` acts on), a second **Tab** moves
focus from the table OUT to the `btn_revoke` button, and **Space**
activates it. The Tab-out-of-table step relies on the Cache table
having `tabKeyNavigation` disabled — Qt's default traps Tab inside the
view, which would make `btn_revoke` unreachable by keyboard with a row
selected. Only once the row is selected and `btn_revoke` is focused do we arm
the D-Bus subscriber and then press **Space** — so the subscriber's timeout
covers just the revoke, not the GUI choreography that precedes it.

```bash
# Re-focus the window so the evdev send-key events land on it.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 0.5

# Shift+Tab: Pending list -> tab bar.
virsh send-key "$VM" --codeset linux --holdtime 100 KEY_LEFTSHIFT KEY_TAB
sleep 0.3
# Right: Pending -> Cache tab.
virsh send-key "$VM" --codeset linux KEY_RIGHT
sleep 0.3
$VMGUI "$VM" screenshot /tmp/22-s2a-cache-tab.png
# Tab: focus into the Cache table.
virsh send-key "$VM" --codeset linux KEY_TAB
sleep 0.3
# Down: select the test.action row (sets currentIndex).
virsh send-key "$VM" --codeset linux KEY_DOWN
sleep 0.3
$VMGUI "$VM" screenshot /tmp/22-s2b-row-selected.png
# Tab: focus OUT of the table to btn_revoke (needs tabKeyNavigation off).
virsh send-key "$VM" --codeset linux KEY_TAB
sleep 0.3

# Arm the signal subscriber NOW — immediately before the revoke — and BLOCK
# until it is actually listening (the --ready file). This keeps its --timeout
# budget scoped to the Space->emit latency only; anchoring it back in S1 (with
# the admin-app launch, six send-keys, and three screenshots in between) is
# what made this scenario flake under full-run load — the 30s window expired
# before Space ever fired the revoke. The subscribe/emit race stays closed
# because we wait for --ready before sending Space, and none of the nav keys
# above trigger a revoke; only Space does. Starting a process on the VM does
# not steal the admin app's GUI focus, so btn_revoke stays focused.
$VMEXEC "$VM" 'setsid python3 /tmp/listen-broker-signal.py ApprovalRevoked \
    --ready /tmp/22-ready --out /tmp/22-signals.json --timeout 30 \
    >/tmp/22-sub.log 2>&1 </dev/null &
  echo $! >/tmp/22-monitor.pid'
$VMEXEC "$VM" 'for i in $(seq 1 50); do [ -f /tmp/22-ready ] && break; sleep 0.1; done; \
  [ -f /tmp/22-ready ] || { echo "subscriber NOT ready"; cat /tmp/22-sub.log; exit 1; }; \
  [ -s /tmp/22-signals.json ] && { echo "premature signal before revoke:"; cat /tmp/22-signals.json; exit 1; }; \
  echo "subscriber ready"'

# Space: activate the focused Revoke button.
virsh send-key "$VM" --codeset linux KEY_SPACE
sleep 1
$VMGUI "$VM" screenshot /tmp/22-s2c-after-revoke.png
```

**Assert**:
- `/tmp/22-s2a-cache-tab.png`: the Cache tab is selected and shows the
  `test.action` row (table is non-empty before revoke).
- The subscriber printed `subscriber ready` (its match rule is installed)
  before the Space keypress, and `/tmp/22-signals.json` was still empty at that
  point — no signal had fired prematurely. (The readiness step `exit 1`s if the
  listener never armed or if a signal was already captured before the revoke,
  so this assertion is executable, not just observational. The helper opens the
  out-file at start-up, so it exists-but-empty until a signal lands — the check
  is `-s`, non-empty, not existence.)
- `/tmp/22-s2c-after-revoke.png`: Cache tab table is empty (no
  `test.action` row).
- No error dialog appeared.

(Screenshots are coarse on this template — AGENTS.md "Ground truth".
The authoritative checks are the captured signal JSON in S3 and the audit
row in S4, which prove the revoke actually fired with the right
payload.)

### S3 — exactly one signal, correct payload

```bash
# The subscriber quits and writes the JSON on the first signal; give it a
# moment, then read the captured payload.
$VMEXEC "$VM" 'sleep 0.5; cat /tmp/22-signals.json 2>/dev/null; echo; cat /tmp/22-sub.log'
```

**Assert** (`/tmp/22-signals.json` is JSON-lines — one JSON object per
captured signal, as written by `listen-broker-signal.py`):
- The file exists and contains exactly **one** line (one signal fired).
- That line is an object whose `args` array is exactly, in order:
  - `2000`  (caller_uid)
  - `"test.action"`  (action)
  - `"/usr/bin/python3.13"`  (exe / match_value)

  i.e. the file reads:
  `{"member": "ApprovalRevoked", "args": [2000, "test.action", "/usr/bin/python3.13"]}`

### S4 — audit row matches

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action, source, approver_uid FROM audit
  WHERE source='revoke' ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: output is exactly `2000|test.action|revoke|1000`.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" '[ -f /tmp/22-monitor.pid ] && kill "$(cat /tmp/22-monitor.pid)" 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/22-signals.json /tmp/22-ready /tmp/22-sub.log /tmp/22-monitor.pid'
APPROVALS_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals;
SQL_EOF
)
AUDIT_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE source='revoke';
SQL_EOF
)
$VMEXEC "$VM" "echo $APPROVALS_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $AUDIT_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- The signal's `exe` argument carries the cache row's `match_value`
  (the exe captured at decide-time), NOT a re-derived live caller exe.
  For a `forever_exe` row that's the same string; for a `forever`
  row `match_value` is empty and the signal carries `string ""`.
  Future scenarios should cover both shapes — this one pins the
  `forever_exe` shape since it's the load-bearing case for qdshell's
  per-exe stream teardown.
- The subscriber (`listen-broker-signal.py`) self-terminates on the first
  captured signal or after `--timeout` seconds, so an orphaned listener
  reaps itself; the teardown still kills the recorded PID and clears the
  JSON/ready/log files explicitly for a clean re-run. Earlier revisions of
  this scenario used `dbus-monitor`, which both raced the revoke at start-up
  and tested the eavesdrop path rather than the ordinary receive policy that
  production subscribers use — the subscriber fixes both.
- Do NOT move the subscriber start back into S1. It is armed in S2 right
  before the Space keypress on purpose: the `--timeout` must cover only the
  revoke's emit latency, not the admin-app launch + keyboard nav + screenshots.
  When it was anchored in S1, slow full-run hosts blew the 30s window before
  the revoke fired, so the signal was emitted to an already-exited listener and
  the scenario falsely read as "broker never emitted ApprovalRevoked". The
  broker's `RevokeApproval` emits unconditionally (the emit is the line right
  before its `[broker] revoked approval ...` journal print), so a missed signal
  here is a harness-timing problem, not a product bug.
- `listen-broker-signal.py` writes JSON-**lines** (one `{"member":..,"args":..}`
  object per signal), NOT a JSON array. Assert against that shape (S3).
