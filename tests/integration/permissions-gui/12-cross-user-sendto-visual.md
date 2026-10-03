# 12 — cross-user send-to, visual admin-app approve

<!-- qci:visual: required -->

**Lane: qdwin** (native Wayland, the shipped launcher). Read the "qdwin lane"
section of `AGENTS.md` first: no xdotool, no `DISPLAY=:0`; graded frames come
from `qdwin_screenshot`.

**What**: trigger a RelayMessage from `work` in the background,
visually confirm the admin approvals app shows the request with
its full detail payload, click **Approve**, verify the payload
landed in `work2`'s notepad.

**Why**: proves the admin half of the thesis under the real UI —
the RelayMessage detail pane must render kind/payload/target_uid,
the "Just this once" radio is pre-selected (forbidden scopes are
broker-rejected), and the Approve click produces a cache-free
audit trail.

Sender-side GUI (qstub-sender as `work`) is deferred.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui            # click-preview / click-confirm
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}

# Session up, work/work2 silo fixtures (the relay target is work2's notepad),
# idle locker held off and proven unlocked. A nonzero exit is a Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup --silos'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user restart qstub-notepad.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_user_unit_active qstub-notepad.service work2'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_socket /run/user/3000/bus'
# The notepad and the work2 relay are Type=simple: wait for their NAMES, and
# for the broker to see the notepad as a receiver, not just for the units.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_dbus_session_name org.qdistro.StubNotepad.uid3000 work2'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_dbus_system_name org.qdistro.UserRelay.uid3000'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_broker_receiver 3000 org.qdistro.StubNotepad.uid3000'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action LIKE 'app.send-to:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 2>/dev/null; true"
# The shipped launcher, first-paint mode: returns only after the window has
# painted and the compositor holds the frame. A nonzero exit is a Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_app'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'
```

## Steps

### S1 — admin app up, pending empty

```bash
qdwin_screenshot "$ART/12-s1-empty.png"
```

**Assert (vision, on the S1 frame)**:
- The admin approvals window is fully drawn (no black, transparent or
 desktop-patterned region cuts through it; on this lane that is a FAIL,
 not a reason to recapture).
- Text `(no selection)` appears in the detail pane.
- Text `Pending` appears (tab label).
- No text starting with `uid=2000` or `app.send-to:` on screen.

### S2 — trigger a RelayMessage as work

```bash
B64=$(base64 -w0 <<'EOF'
set -e
runuser -u work -- dbus-send --system --print-reply \
 --dest=org.qdistro.AdminBroker1 \
 /org/qdistro/AdminBroker1 \
 org.qdistro.AdminBroker1.RelayMessage \
 int32:3000 \
 string:org.qdistro.StubNotepad.uid3000 \
 string:text/plain \
 string:hello_visual \
 >/tmp/12-relay.out 2>&1 &
echo $! >/tmp/12-relay.pid
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# The request is in the broker AND displayed as the one row. A timeout FAILS S2.
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh; await_broker_pending_action 'app.send-to:3000:org.qdistro.StubNotepad.uid3000' 30"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals \(1 pending\)" 30'
qdwin_screenshot "$ART/12-s2-pending.png"
```

**Assert (vision, on `12-s2-pending.png`)** — every bullet below is
a substring that must be visible somewhere on screen; match on the
core words, not character-perfect alignment:
- `uid=2000` (detail pane header).
- `app.send-to:3000:org.qdistro.StubNotepad.uid3000` (action line).
- `kind=text/plain`, `payload=hello_visual`, `target_uid=3000`,
 `target_service=org.qdistro.StubNotepad.uid3000` (all four keys
 in the details line — the broker detail sanitiser may join them
 with `,` and the font may wrap, but each key=value substring
 must be present).
- `Just this once` label with its radio in the filled state (the
 scope picker default).
- Buttons labeled `Approve` and `Deny` are present.

### S3 — click Approve

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
# Runner: locate the "Approve" button in 12-s2-pending.png (there is exactly
# one, next to "Deny" below the scope group) and click its centre with the
# preview / confirm handshake (AGENTS.md 3b):
#   $VMGUI "$VM" click-preview <cx> <cy> "Approve"
#   $VMGUI "$VM" click-confirm <preview-manifest>
# Then wait for the list to empty before the frame. A timeout FAILS S3.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'
qdwin_screenshot "$ART/12-s3-approved.png"
```

**Assert (vision, on `12-s3-approved.png`)**:
- `(no selection)` is visible again.
- No text starting with `uid=2000` or `app.send-to:` on screen.

### S4 — delivery + audit + no-cache

```bash
$VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply \
 --dest=org.qdistro.StubNotepad.uid3000 \
 /org/qdistro/App1 \
 org.qdistro.App1.GetDocument'

SQL_AUDIT_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action, decision, scope, source, approver_uid
 FROM audit ORDER BY id DESC LIMIT 1;
SQL_EOF
)
SQL_COUNT_B64=$(base64 -w0 <<'SQL_EOF'
SELECT count(*) FROM approvals WHERE action LIKE 'app.send-to:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_AUDIT_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
$VMEXEC "$VM" "echo $SQL_COUNT_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
```

**Assert**:
- GetDocument output contains the substring `[text/plain] hello_visual`.
- Audit row equals
 `2000|app.send-to:3000:org.qdistro.StubNotepad.uid3000|1|once|prompt|1000`.
- Approvals-row count is `0` — one_shot actions never cache.

## Teardown

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user restart qstub-notepad.service'
$VMEXEC "$VM" 'rm -f /tmp/12-relay.out /tmp/12-relay.pid'
```

## Notes for the runner

- S2's `dbus-send` has a 25s default reply timeout. If you take
 longer than that between S2 and S3, the sender gets NoReply and
 its process exits — the broker has already recorded the pending,
 and the admin click still delivers the payload + audit row
 (the reply just goes nowhere). S4's assertions verify the
 broker and notepad state, not the sender's exit.
- Don't hard-code pixel coordinates for the Approve click. The
 admin app's layout shifts with Qt font/DPI/theme; locate the
 `Approve` text in the frame you just took.
