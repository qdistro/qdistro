# 04 — Qt admin app Ctrl+Y approves with selected scope

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

### S1 — launch admin app, inject one pending request, pick "1 hour"

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3

B64=$(base64 -w0 <<'EOF'
#!/bin/bash
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
 >/tmp/work1.log 2>&1 & echo $! >/tmp/work1.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2

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
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
 --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/04-qt-admin-app-approve-s1-pre-select.png
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_LEFTSHIFT KEY_2
sleep 0.3
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
 xdotool search --sync --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 1
$VMGUI "$VM" screenshot /tmp/04-qt-admin-app-approve-s2-afterapprove.png

# Confirm the SDK-side process actually got ALLOWED (not just that
# the list emptied).
$VMEXEC "$VM" 'wait $(cat /tmp/work1.pid) 2>/dev/null; cat /tmp/work1.log'
```

**Assert (after approve):**
- Screenshot shows empty list, detail pane back to `(no selection)`.
- The SDK log contains `ALLOWED` (not `DENIED`) on its own line.
 `test_permission.py` prints one or the other based on the broker's
 decision; this is the ground truth that the broker allowed it.

### S3 — same request returns cache-hit, no new pending row

```bash
# A second call with the same uid/action/exe should be short-circuited
# by the 1-hour cache entry written in S2. Admin app should see no
# new pending row appear.
B64=$(base64 -w0 <<'EOF'
#!/bin/bash
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
 >/tmp/work2.log 2>&1 & echo $! >/tmp/work2.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/04-qt-admin-app-approve-s3-cachehit.png
$VMEXEC "$VM" 'wait $(cat /tmp/work2.pid) 2>/dev/null; cat /tmp/work2.log'
```

**Assert (cache hit):**
- Screenshot still shows empty list (no new pending row appeared).
- `/tmp/work2.log` contains `ALLOWED`, confirming the SDK returned
 true via the cache path without any admin interaction.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 2>/dev/null; true"
$VMEXEC "$VM" 'rm -f /tmp/work1.log /tmp/work1.pid /tmp/work2.log /tmp/work2.pid /tmp/admin-app.log'
```

## Notes for the runner

- S3 relies on S2 having written a cache row. If S2 FAILs, S3 is
 meaningless — report S2's FAIL and skip S3 rather than chaining
 another PASS/FAIL judgment on a broken precondition.
- The cache-row DELETE in Setup + Teardown keeps this scenario
 isolated across runs. Don't skip it — a leftover 1h row makes S1
 into a cache hit and the admin app never sees a pending row.
