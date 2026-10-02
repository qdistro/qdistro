# 06 — Qt admin app full mouse path: click radio, click Approve

<!-- qci:visual: required -->

**Lane: qdwin** (native Wayland, the shipped launcher). Read the "qdwin lane"
section of `AGENTS.md` first: no xdotool, no `DISPLAY=:0`; graded frames come
from `qdwin_screenshot`.

**What**: cover the primary mouse interaction path end to end — select
the "1 hour" scope by clicking its radio, commit the decision by
clicking the Approve button, verify the SDK got `ALLOWED` and the
cache hit short-circuits a second request.

**Why**: scenario 04 covers the keyboard path (Ctrl+Shift+2 + Ctrl+Y).
A non-keyboard-first admin using a mouse is equally supported; a
regression in mouse handling (e.g. a radio button group going
non-clickable, Approve wired to the wrong slot) would pass the
keyboard scenario but break this one. Both paths must stay green.

This scenario uses intent-level mouse instructions (AGENTS.md 3b).
The runner takes screenshots, visually locates the target widgets,
generates an ImageMagick `click-preview` with the coordinates it computes,
visually confirms the moved cursor and red ring, then issues `click-confirm` —
no hardcoded pixel offsets, portable across Qt font / DPI / theme changes.
On the qdwin lane the click goes through the QEMU pointer into qdwin, which
delivers it to the native Wayland window under it; the RESULT of each click
is graded from a `qdwin_screenshot`, not from the click-confirm post frame.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui            # click-preview / click-confirm
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}

# Session up, work/work2 silo fixtures, idle locker held off and proven
# unlocked. A nonzero exit is a Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup --silos'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 2>/dev/null; true"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'
```

## Steps

### S1 — launch admin app, inject request, click "1 hour" radio

```bash
# The shipped launcher, first-paint mode (a nonzero exit FAILS S1).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_app'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'

B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start work1 work 'python3 /usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# The request is in the broker AND displayed as the one row.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_broker_pending_action test.action 30'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals \(1 pending\)" 30'

# Focus the admin-approvals window, take a baseline screenshot, then
# CLICK THE "1 HOUR" RADIO BUTTON.
#
# Runner: read /tmp/06-qt-admin-app-mouse-s1a-baseline.png, locate
# the "1 hour" radio row in the Scope group (second radio, between
# "Just this once" and "24 hours"), compute the click point on the
# radio's bullet or label, then use `click-preview` / `click-confirm`.
# See AGENTS.md.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
qdwin_screenshot "$ART/06-qt-admin-app-mouse-s1a-baseline.png"

# >>> Runner: take over here. Read the baseline screenshot, preview the
# proposed target, visually confirm its red ring, click-confirm the "1 hour"
# radio, then (a radio tick publishes no state a waiter can read) settle
# briefly and capture:
sleep 1
qdwin_screenshot "$ART/06-qt-admin-app-mouse-s1b-1h-selected.png"
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
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'

# >>> Runner: using a fresh screenshot, locate the "Approve" button, generate
# and visually confirm its marked preview, click-confirm it, then wait for the
# list to empty and capture. A title-wait timeout FAILS S2.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'
qdwin_screenshot "$ART/06-qt-admin-app-mouse-s2-afterapprove.png"

# bg_wait, never `wait $(cat X.pid)` — that does not wait in a separate guest
# shell (AGENTS.md, "A backgrounded job"). A TIMEOUT here IS this step's failure.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait work1 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log work1; echo "rc=$(bg_rc work1)"'
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
source /tmp/qci-gui-waiters.sh
bg_start work2 work 'python3 /usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# A cache hit returns without a prompt, so the caller FINISHING is the
# readiness signal. bg_wait, never `wait $(cat X.pid)` — that does not wait in
# a separate guest shell (AGENTS.md, "A backgrounded job"). A TIMEOUT here IS
# this step's failure (a prompt appeared and nobody answered it).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait work2 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log work2; echo "rc=$(bg_rc work2)"'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 10'
qdwin_screenshot "$ART/06-qt-admin-app-mouse-s3-cachehit.png"
```

**Assert (cache hit):**
- Screenshot still shows empty list — no new pending row.
- `/tmp/work2.log` contains `ALLOWED`, confirming cache short-circuit.

## Teardown

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 2>/dev/null; true"
$VMEXEC "$VM" 'rm -f /tmp/work1.log /tmp/work1.pid /tmp/work2.log /tmp/work2.pid'
```

## Notes for the runner

- This is the canonical mouse scenario. Scenario 04 covers the
 keyboard-driven equivalent with the same S3 cache-hit assertion.
 If both pass, the approve + cache path is covered for both
 interaction modes.
- If an S1 or S2 preview ring misses its target, adjust once and generate a
 second preview without clicking the first. If the second preview is still
 wrong, FAIL with both annotated images and coordinate rows — two tries then
 stop.
- The S3 cache-hit precondition is S2 having written a 1-hour row.
 If S2 FAILs, S3 will fail too; report both honestly.
