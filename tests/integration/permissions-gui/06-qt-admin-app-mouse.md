# 06 — Qt admin app full mouse path: click radio, click Approve

**What**: cover the primary mouse interaction path end to end — select
the "1 hour" scope by clicking its radio, commit the decision by
clicking the Approve button, verify the SDK got `ALLOWED` and the
cache hit short-circuits a second request.

**Why**: scenario 04 covers the keyboard path (Tab + Down + Ctrl+Y).
A non-keyboard-first admin using a mouse is equally supported; a
regression in mouse handling (e.g. a radio button group going
non-clickable, Approve wired to the wrong slot) would pass the
keyboard scenario but break this one. Both paths must stay green.

This scenario uses intent-level mouse instructions (AGENTS.md ).
The runner takes screenshots, visually locates the target widgets,
and issues `vm-gui click` with coordinates it computes — no
hardcoded pixel offsets, portable across Qt font / DPI / theme
changes.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 2>/dev/null; true"
sleep 1
```

## Steps

### S1 — launch admin app, inject request, click "1 hour" radio

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

# Focus the admin-approvals window, take a baseline screenshot, then
# CLICK THE "1 HOUR" RADIO BUTTON.
#
# Runner: read /tmp/06-qt-admin-app-mouse-s1a-baseline.png, locate
# the "1 hour" radio row in the Scope group (second radio, between
# "Just this once" and "24 hours"), compute the click point on the
# radio's bullet or label, then `vm-gui click X Y`. See AGENTS.md .
B64=$(base64 -w0 <<'EOF'
#!/bin/bash
runuser -u admin -- env DISPLAY=:0 \
 xdotool search --sync --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/06-qt-admin-app-mouse-s1a-baseline.png

# >>> Runner: take over here. Read the baseline screenshot, click
# the "1 hour" radio, then capture:
$VMGUI "$VM" screenshot /tmp/06-qt-admin-app-mouse-s1b-1h-selected.png
```

**Assert (1h selected via click):**
- Baseline screenshot (`s1a`) shows the admin-approvals window with
 one pending row `uid=2000 test.action` selected in the left list,
 detail pane populated, and `Just this once` as the active radio.
- Post-click screenshot (`s1b`) shows the `1 hour` radio with its
 bullet filled and `Just this once` empty. A focus rectangle
 around `1 hour` is acceptable evidence too.
- The pending row in the left list is still there (approve hasn't
 happened yet; scope selection doesn't decide).

### S2 — click the Approve button, verify ALLOWED

```bash
# Focus check again in case clicking the radio changed it.
B64=$(base64 -w0 <<'EOF'
#!/bin/bash
runuser -u admin -- env DISPLAY=:0 \
 xdotool search --sync --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# >>> Runner: using the s1b screenshot (or a fresh one if you prefer),
# locate the "Approve" button, click it, then capture:
$VMGUI "$VM" screenshot /tmp/06-qt-admin-app-mouse-s2-afterapprove.png

$VMEXEC "$VM" 'wait $(cat /tmp/work1.pid) 2>/dev/null; cat /tmp/work1.log'
```

**Assert (approved via click):**
- Screenshot shows the left list empty; detail pane back to
 `(no selection)`.
- `/tmp/work1.log` contains `ALLOWED` on its own line — ground
 truth from the SDK that the broker allowed the request.
- No error dialog / red banner.

### S3 — second request returns cache-hit, no new pending row

```bash
# Same pattern as scenario 04's S3 — a second call with the same
# uid/action/exe should be short-circuited by the 1-hour cache row
# written in S2. Admin app should see no new pending row.
B64=$(base64 -w0 <<'EOF'
#!/bin/bash
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
 >/tmp/work2.log 2>&1 & echo $! >/tmp/work2.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/06-qt-admin-app-mouse-s3-cachehit.png
$VMEXEC "$VM" 'wait $(cat /tmp/work2.pid) 2>/dev/null; cat /tmp/work2.log'
```

**Assert (cache hit):**
- Screenshot still shows empty list — no new pending row.
- `/tmp/work2.log` contains `ALLOWED`, confirming cache short-circuit.

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

- This is the canonical mouse scenario. Scenario 04 covers the
 keyboard-driven equivalent with the same S3 cache-hit assertion.
 If both pass, the approve + cache path is covered for both
 interaction modes.
- If S1 or S2 click misses, take a second screenshot to see where
 the cursor landed and adjust once. If still wrong, FAIL with the
 coordinates you used — two tries then stop.
- The S3 cache-hit precondition is S2 having written a 1-hour row.
 If S2 FAILs, S3 will fail too; report both honestly.
