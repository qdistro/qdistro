# 04 — Qt admin app Ctrl+Y approves with selected scope

<!-- qci:visual: required -->

**What**: with one pending request and the "1 hour" scope radio
selected, press Ctrl+Y and verify the work process was allowed (not
denied), the list returns to empty, and the approval was cached (a
subsequent request with the same uid/action/exe returns immediately
without prompting).

**Why**: scenario 03 covers the deny path (Ctrl+N). The approve
path has the opposite failure mode — a silent downgrade to deny
would look identical to "the shortcut didn't fire" until the calling
user notices their operation failed. This scenario distinguishes
"shortcut fired, approved" from "shortcut didn't fire" by checking
the SDK return value and the cache.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-0052}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
# Broker restart drains any stale pending AND clears the sqlite-backed
# scope cache of prior "test.action" entries from earlier runs (cache
# is persistent; restart alone does not wipe it — we clear explicitly).
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 2>/dev/null; true"
sleep 1
```

## Steps

**Readiness is mechanical here — run every `await_*` line as written.**
Each capture below is preceded by a waiter on the admin window's title,
which the app computes from the rows its Pending list actually displays
(`admin approvals (N pending)`, or `admin approvals` when empty;
`_update_window_title` in `qdistro_admin_app.py`). A waiter that TIMES OUT
is that step's failure — record it and keep the frame as evidence. Do not
replace a waiter with a sleep, drop it, or write a readiness loop of your
own; that is what lost the 2026-09-30 full run (captures taken with no
settle, then a hand-written waiter that stalled).

### S1 — launch admin app, inject one pending request, pick "1 hour"

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
# Ready 1: the admin window is up with an EMPTY Pending list.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_x11_window_title admin "admin approvals" 45'

B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start work1 work 'python3 /usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# Ready 2: the request is in the broker AND displayed as the one row.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_broker_pending_action test.action 30'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_x11_window_title admin "admin approvals \(1 pending\)" 30'

# Select the "1 hour" scope via its dedicated keyboard shortcut.
# The admin app binds Ctrl+Shift+1..8 directly to the scope radios
# (once/1h/24h/forever/...), so Ctrl+Shift+2 ticks the "1 hour"
# radio regardless of widget-tree layout or focus order. Earlier
# revisions used Tab→Down keyboard nav, which broke when the widget
# tree shifted (TAB walked the tab strip or button row and never
# landed on the scope group); an OCR-targeted click on the label is
# also fragile (radio-bullet offset guesswork). The scope keys are
# unguarded (they only tick a radio and commit nothing) so they take
# effect immediately, and the chord is delivered via `virsh send-key`
# — the blessed input path for modifier chords on XWayland Qt apps.
# See `tests/integration/permissions-gui/AGENTS.md`. The binding lives
# in `qdistro_admin_app.py` at the `_mk_shortcut("Ctrl+Shift+{i+1}", ...)`
# scope loop.
B64=$(base64 -w0 <<'EOF'
#!/bin/bash
# Bounded: a bare `--sync` waits forever when no X window matches.
runuser -u admin -- env DISPLAY=:0 timeout 15 xdotool search --sync \
 --name "admin approvals" windowactivate --sync \
 || { echo "admin approvals window did not activate within 15s" >&2; exit 1; }
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/04-qt-admin-app-approve-s1-pre-select.png
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_LEFTSHIFT KEY_2
# A radio tick publishes no state a waiter can read; this frame is
# corroboration only. The scope is PROVED after S2 by the cache row
# (scope=1h, a 3600 s lifetime), which only the 1 hour radio produces.
sleep 1
$VMGUI "$VM" screenshot /tmp/04-qt-admin-app-approve-s1-1h-selected.png
```

**Assert (1h selected):**
- List shows the pending `uid=2000 test.action` row selected.
- In the scope group, the `1 hour` radio is filled (selected) and
 `Just this once` is no longer filled.

### S2 — Ctrl+Y, confirm approval lands

```bash
# Modifier combo must go via KVM keyboard (AGENTS.md ).
B64=$(base64 -w0 <<'EOF'
#!/bin/bash
runuser -u admin -- env DISPLAY=:0 \
 timeout 15 xdotool search --sync --name "admin approvals" windowactivate --sync \
 || { echo "admin approvals window did not activate within 15s" >&2; exit 1; }
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
# Ready 3: the Pending list is empty again before the frame is taken.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_x11_window_title admin "admin approvals" 30'
$VMGUI "$VM" screenshot /tmp/04-qt-admin-app-approve-s2-afterapprove.png

# Confirm the SDK-side process actually got ALLOWED (not just that
# the list emptied).
# bg_wait, never `wait $(cat X.pid)` — that does not wait in a separate guest
# shell (AGENTS.md, "A backgrounded job"). A TIMEOUT here IS this step's failure.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait work1 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log work1; echo "rc=$(bg_rc work1)"'

# The approval that S2 committed, as the broker cached it: the scope the
# admin picked, and its lifetime. Expected: `2000|1|1h|3600`.
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, decision, scope, COALESCE(expires_at - created_at, -1)
  FROM approvals WHERE action='test.action' ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 -separator '|' /var/lib/qdistro/approvals/approvals.sqlite"
```

**Assert (after approve):**
- Screenshot shows empty list, detail pane back to `(no selection)`.
- The cache row printed last is exactly `2000|1|1h|3600`: an approval
 (decision 1) for uid 2000 at the 1 hour scope. Any other scope means the
 Ctrl+Shift+2 tick did not land before Ctrl+Y — FAIL, whatever the S1
 frame looked like.
- The SDK log contains `ALLOWED` (not `DENIED`) on its own line.
 `test_permission.py` prints one or the other based on the broker's
 decision; this is the ground truth that the broker allowed it.

### S3 — same request returns cache-hit, no new pending row

```bash
# A second call with the same uid/action/exe should be short-circuited
# by the 1-hour cache entry written in S2. Admin app should see no
# new pending row appear.
# Audit baseline BEFORE the second call: S3 proves the cache hit from the
# broker's own audit rows written after this id, not from the end state.
$VMEXEC "$VM" "sqlite3 /var/lib/qdistro/audit/audit.sqlite 'SELECT COALESCE(MAX(id),0) FROM audit;' > /tmp/04-s3.baseid"
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start work2 work 'python3 /usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# A cache hit returns without a prompt, so the caller FINISHING is the
# readiness signal: wait for it, then prove the list stayed empty.
# bg_wait, never `wait $(cat X.pid)` — that does not wait in a separate guest
# shell (AGENTS.md, "A backgrounded job"). A TIMEOUT here IS this step's
# failure (a prompt appeared and nobody answered it).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait work2 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log work2; echo "rc=$(bg_rc work2)"'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_x11_window_title admin "admin approvals" 10'
$VMGUI "$VM" screenshot /tmp/04-qt-admin-app-approve-s3-cachehit.png

# Every audit row for test.action since the baseline, as
# caller_uid|decision|source. A cache hit writes exactly one row with
# source=cache (qdistro_admin_broker.py, _record_check from the cache
# lookup); a prompted decision writes source=prompt. Expected output is
# exactly the single line `2000|1|cache`.
$VMEXEC "$VM" 'b=$(cat /tmp/04-s3.baseid); sqlite3 -separator "|" /var/lib/qdistro/audit/audit.sqlite "SELECT caller_uid, decision, source FROM audit WHERE id > $b AND action = '"'"'test.action'"'"' ORDER BY id;"'
```

**Assert (cache hit):**
- Screenshot still shows empty list (no new pending row appeared).
- `/tmp/work2.log` contains `ALLOWED`, confirming the SDK returned
 true via the cache path without any admin interaction.
- The audit query prints exactly one line, `2000|1|cache`. This is the
 mechanical proof of the cache hit: a `prompt` row (or two rows) means a
 new pending request was created, even if the list is empty again by
 the time the frame is taken — FAIL. An empty result or a missing
 baseline file is ERROR (the proof could not be taken), never a pass.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 2>/dev/null; true"
$VMEXEC "$VM" 'rm -f /tmp/work1.log /tmp/work1.pid /tmp/work2.log /tmp/work2.pid /tmp/04-s3.baseid /home/admin/.local/state/qdistro/admin-app.log'
```

## Notes for the runner

- S3 relies on S2 having written a cache row. If S2 FAILs, S3 is
 meaningless — report S2's FAIL and skip S3 rather than chaining
 another PASS/FAIL judgment on a broken precondition.
- The cache-row DELETE in Setup + Teardown keeps this scenario
 isolated across runs. Don't skip it — a leftover 1h row makes S1
 into a cache hit and the admin app never sees a pending row.
