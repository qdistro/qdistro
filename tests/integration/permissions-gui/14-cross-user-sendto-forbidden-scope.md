# 14 — cross-user send-to, admin picks non-once scope → ScopeNotPermitted

<!-- qci:visual: required -->

**Lane: qdwin** (native Wayland, the shipped launcher). Read the "qdwin lane"
section of `AGENTS.md` first: no xdotool, no `DISPLAY=:0`; graded frames come
from `qdwin_screenshot`; every click uses the preview / confirm handshake
(AGENTS.md 3b).

**What**: admin receives a RelayMessage, picks `1 hour` via the
scope radio group, clicks **Approve**. Broker rejects the forbidden
one-shot scope; admin app surfaces this in a modal
`Decision not recorded` dialog. Admin dismisses, picks
`Just this once`, clicks Approve — delivery succeeds.

**Why**: the one-shot policy (`_ONESHOT_FORBIDDEN_SCOPES`) is a
security contract — a silent downgrade to "1h caches the grant"
would widen a single send-to approval into a wildcard for the
next hour. This scenario catches a broker regression where the
forbidden-scope check gets deleted or inverted, AND pins the
admin app's error-surfacing UX (the rejection MUST be visible
or the admin would assume the click worked).

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
# Establish work2's user manager before addressing its units. A linger-enabled
# account can still be between manager teardown and startup under an 8-worker
# GUI run; `systemctl --machine` then fails with a misleading transport error.
$VMEXEC "$VM" 'loginctl enable-linger work2 && systemctl start user@3000.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_socket /run/user/3000/bus 30 1'
$VMEXEC "$VM" 'runuser -u work2 -- env XDG_RUNTIME_DIR=/run/user/3000 \
 systemctl --user restart qstub-notepad.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && \
 await_user_unit_active qstub-notepad.service work2 30 1'
# `active` only means the process was forked. S4 addresses this stub DIRECTLY
# (`--dest=org.qdistro.StubNotepad.uid3000`), and the stub ships no .service
# activation file, so reaching it before it has claimed its name fails with a
# TERMINAL `ServiceUnknown: The name is not activatable`. Wait for the name
# itself, which is the condition S4 actually depends on.
#
# HONEST ATTRIBUTION: this is a real gap in the gate, but it is NOT what made
# S4 ERROR in full-20260911T070416Z. There the agent's whole Setup exec died
# host-side in 0ms on a mangled quoting construct and was never retried, so
# this stub was never started at all -- `diagnostics.txt` shows
# `qstub-notepad.service ... inactive (dead)` with no `since`. That failure is
# an agent-driving defect (a nonzero Setup exit read as "continue"), logged for
# the qci1 classifier work, not something a waiter can fix.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && \
 await_dbus_session_name org.qdistro.StubNotepad.uid3000 work2 30 1'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_dbus_system_name org.qdistro.UserRelay.uid3000'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_broker_receiver 3000 org.qdistro.StubNotepad.uid3000'
# The shipped launcher, first-paint mode (a nonzero exit is a Setup ERROR).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_app'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'
```

## Steps

### S1 — trigger RelayMessage

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
 string:scope_test_payload \
 >/tmp/14-relay.out 2>&1 &
echo $! >/tmp/14-relay.pid
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# The app displays the request as its one row. A timeout FAILS S1.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals \(1 pending\)" 30'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
qdwin_screenshot "$ART/14-s1-pending.png"
```

Open S1 before targeting any control. On this lane the frame after the
title wait is the app's committed state: a black or desktop-patterned
rectangle over the detail pane is a FAIL, not a reason to recapture.

**Assert (vision, on `14-s1-pending.png`)**:
- `uid=2000` and `app.send-to:3000:org.qdistro.StubNotepad.uid3000` visible.
- `payload=scope_test_payload` visible.
- Scope labels `Just this once`, `1 hour`, `24 hours`, `Forever`
 all visible (default radio is `Just this once`).

### S2 — select "1 hour" with a click

```bash
# Runner: in 14-s1-pending.png find the visible text "1 hour". The clickable
# radio is immediately to the LEFT of that label (~15 px left of the label's
# left edge, at its vertical centre). Click it with the handshake:
#   $VMGUI "$VM" click-preview <cx> <cy> "1 hour radio"
#   $VMGUI "$VM" click-confirm <preview-manifest>
# A radio tick publishes no state a waiter can read: settle briefly, capture.
sleep 1
qdwin_screenshot "$ART/14-s2-1hour-selected.png"
```

**Assert (vision, on `14-s2-1hour-selected.png`)**:
- `1 hour` still visible (no layout collapse), and its radio reads as
 filled. If the glyph state is genuinely ambiguous, S3 pins the state
 indirectly (only a non-once scope can trigger ScopeNotPermitted).

### S3 — click Approve, expect forbidden-scope modal

```bash
# Runner: find the "Approve" button in a fresh frame and click it with the
# preview / confirm handshake. The refusal modal is its own toplevel titled
# `Decision not recorded`; wait for the compositor to map it (a timeout
# FAILS S3: no modal means the admin never learns the decision was refused).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "Decision not recorded" 30'
qdwin_screenshot "$ART/14-s3-rejected.png"
```

**Assert (vision, on `14-s3-rejected.png`)**:
- A modal with heading text `Decision not recorded` has appeared.
- The modal body makes the forbidden-scope reason visible. Accept either
 the legacy raw exception text (`ScopeNotPermitted` / `scope '1h' not
 permitted for one-shot`) or the current friendly copy (`Scope not
 permitted` / `not permitted for one-shot`). Do not require the raw
 exception class name; the GUI may redact it while preserving the
 operator-visible reason.
- Behind the modal (it may be greyed out), the pending list row
 (`uid=2000 app.send-to:3000:...`) is still visible — the
 request was NOT decided; admin gets another chance.
- The modal has a button labeled `OK` (or similar dismiss).

### S4 — dismiss modal, retry with "Just this once"

```bash
# Runner, each click with the preview / confirm handshake:
# 1. In 14-s3-rejected.png find the modal's "OK" button and click it. The
#    modal must go away before you target the main window:
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; for i in $(seq 1 60); do qdwin_window_handle "Decision not recorded" >/dev/null || exit 0; sleep 0.5; done; echo "modal still mapped" >&2; exit 1'
# 2. Take a fresh frame, find "Just this once", click the radio ~15 px left
#    of that label:
qdwin_screenshot "$ART/14-s4a-modal-dismissed.png"
# 3. Find "Approve" and click it. Then wait for the list to empty (a timeout
#    FAILS S4) and capture:
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'
qdwin_screenshot "$ART/14-s4-once-approved.png"
```

**Assert (vision, on `14-s4-once-approved.png`)**:
- `(no selection)` visible.
- No modal on screen.
- No `uid=2000` / `app.send-to:` text remaining.

```bash
# Side-effect assertions.
$VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply \
 --dest=org.qdistro.StubNotepad.uid3000 \
 /org/qdistro/App1 \
 org.qdistro.App1.GetDocument'

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT decision, scope FROM audit
 WHERE action LIKE 'app.send-to:%' ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**:
- GetDocument output contains `scope_test_payload`.
- Latest audit row for `app.send-to:%` has `decision=1` and
 `scope=once`.

## Teardown

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'rm -f /tmp/14-relay.out /tmp/14-relay.pid'
```

## Notes for the runner

- S3's modal is the critical assertion: it's the only place an
 admin learns that the broker refused their scope choice. If the
 modal doesn't appear, report FAIL before attempting S4 — the
 admin app's `on_decide_failed` handler (or equivalent) is
 broken and the whole scope-enforcement UX collapses.
- S2's assertion on radio-filled-state is intentionally soft; S3's
 forbidden-scope modal proves that the broker saw a `1h` scope,
 which can only happen if the radio actually flipped.
- S1's `dbus-send` keeps its 25 s default reply timeout: the sender may
 give up with NoReply before S4; S4's assertions read the broker and
 notepad, not the sender's exit.
