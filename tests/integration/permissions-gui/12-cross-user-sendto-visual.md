# 12 — cross-user send-to, visual admin-app approve

<!-- qci:visual: required -->

**Lane: qdwin** (native Wayland, the shipped launcher). Read the "qdwin lane"
section of `AGENTS.md` first: no xdotool, no `DISPLAY=:0`; graded frames come
from `qdwin_screenshot`.

**What**: trigger a RelayMessage from `work` in the background,
visually confirm the admin approvals app shows the request with
its full detail payload, click **Approve**, verify the payload
landed in `work2`'s notepad. Then do the same with two REAL
`qnotebook` instances (work and work2): both are discoverable
receivers, the admin app renders the qnotebook request, and the
Approve click delivers to work2's qnotebook.

**Why**: proves the admin half of the thesis under the real UI —
the RelayMessage detail pane must render kind/payload/target_uid,
the "Just this once" radio is pre-selected (forbidden scopes are
broker-rejected), and the Approve click produces a cache-free
audit trail.

Sender-side GUI (qstub-sender as `work`) is deferred.

The real-qnotebook leg (S5–S8) is the former permissions-gui/16, merged
here: the real-app action string (`app.send-to:3000:org.qdistro.Qnotebook.uid3000`)
through the shipped admin GUI, discovery of BOTH real apps, delivery to the
real receiver, and an approver-uid-1000 audit row. The receivers run
offscreen on their own session buses. Retired with the labwc lane: 16's S1
check that a qnotebook editor window was visible beside the admin app. That
visibility only ever came from the shared-XWayland `xhost` expedient (any
silo user could drive any other), which the product does not have; visible
silo-app rendering belongs to a product launch path, not this scenario.

## Driver synchronization contract

Run all guest-side Setup, waiters, and Teardown commands in the single claimed
root guest driver required by the qci scenario prompt. Its first commands are
`source /tmp/qci-gui-waiters.sh` and
`qci_claim_driver /tmp/qci/qdistro_tests_integration_permissions-gui_12-cross-user-sendto-visual.md/driver.lock`.
Keep that same driver alive through the S1–S8 host gates. After the S3 host
capture/click gate is released, the guest driver must stop at
`qci_host_step s4-delivery`; only the host runner performs the S4 D-Bus/sqlite
queries while it is paused. After the host releases the exact S4 token the
driver runs S5's setup and stops at `qci_host_step s5-qnb-receivers`; after
S6's request it stops at `qci_host_step s6-qnb-pending`; and after the S7
click it stops at `qci_host_step s8-qnb-delivery`, where only the host runs the
S8 queries. Teardown runs in that driver only after the host releases the
exact S8 token, and `qci_claim_done` is the driver's final command.

Put `qci_host_step s1-empty` immediately before the S2 RelayMessage, and
`qci_host_step s2-pending` after the S2 broker and window-title waits. At each
gate, the host runner must capture and inspect the frame before creating that
gate's `.go` directory. Run capture, visual inspection, and gate release as
separate commands; never append `; mkdir <token>.go` to a capture command. A
failed helper import or failed capture must leave the guest paused. In
`full-20261003T150632Z-1041444`, a failed S1 capture command still ran a
later `; mkdir`, so the eventual S1 frame showed the S2 request.

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
# Pending requests live in the broker's memory; sqlite cache cleanup does not
# clear them. Check the broker model before the app is launched.
PENDING_B64=$(base64 -w0 <<'PYEOF'
import dbus, sys
bus = dbus.SystemBus()
obj = bus.get_object("org.qdistro.AdminBroker1", "/org/qdistro/AdminBroker1")
pending = dbus.Interface(obj, "org.qdistro.AdminBroker1").GetPending()
print(f"pending_count={len(pending)}")
if pending:
    print("FAIL(setup): GetPending not empty after broker restart", file=sys.stderr)
    sys.exit(1)
PYEOF
)
$VMEXEC "$VM" "echo $PENDING_B64 | base64 -d | runuser -u admin -- python3 -"
SQL_B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh || exit 2
qci_sqlite approvals "DELETE FROM approvals WHERE action LIKE 'app.send-to:%';" 2>/dev/null || true
EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | bash"
# The shipped launcher, first-paint mode: returns only after the window has
# painted and the compositor holds the frame. A nonzero exit is a Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_app'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'
```

## Steps

### S1 — admin app up, pending empty

```bash
qdwin_screenshot "$ART/12-s1-empty.png" || exit 1
```

Check all S1 assertions while the driver waits at `s1-empty`. Record the
verdict, then release that exact gate so the guest may send the S2 request.
If capture fails, keep the gate closed and report the capture error.

**Assert (vision, on the S1 frame)**:
- The admin approvals window is fully drawn (no black, transparent or
 desktop-patterned region cuts through it; on this lane that is a FAIL,
 not a reason to recapture).
- Text `(no selection)` appears in the detail pane.
- Text `Pending` appears (tab label).
- No text starting with `uid=2000` or `app.send-to:` on screen.
- Setup printed `pending_count=0` before the app was launched.

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

**Critical ordering:** after S3's screenshot and visual check, keep the guest
driver paused at this guest-side barrier:

```bash
# In the root guest driver, after the host has captured and checked S3:
qci_host_step s4-delivery
```

Poll `/tmp/qci/qdistro_tests_integration_permissions-gui_12-cross-user-sendto-visual.md/waiting`
through `vm-exec` until it contains the fresh `s4-delivery.*` token. Do not
create that token's `.go` directory yet. While the guest is paused, run every
S4 D-Bus and sqlite query below, record the outputs, and evaluate all four
assertions. Only then run `vm-exec "$VM" "mkdir
/tmp/qci/qdistro_tests_integration_permissions-gui_12-cross-user-sendto-visual.md/<exact-token>.go"`
with the exact token read from `waiting`. Teardown restarts the notepad and
destroys its in-memory document, so neither the guest driver nor a teardown
trap may run before S4 is complete.

The delivery call is asynchronous inside the notepad (Qt queues the append on
its event loop). Poll `GetDocument` for at most 10 seconds, saving each result
under `$ART`, until the exact payload appears. Each D-Bus call has a 1-second
reply timeout. Capture the notepad service PID before and after that poll; it
must remain the same nonzero PID throughout. This distinguishes a delayed
append from a receiver restart. Do not treat the audit row as delivery proof:
the broker writes it before forwarding.

```bash
NOTEPAD_PID_BEFORE=$($VMEXEC "$VM" \
 'systemctl --machine=work2@.host --user show qstub-notepad.service -p MainPID --value')
GETDOC=
deadline=$((SECONDS + 10))
attempt=0
while [ "$SECONDS" -lt "$deadline" ]; do
  attempt=$((attempt + 1))
  GETDOC=$($VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply \
 --reply-timeout=1000 \
 --dest=org.qdistro.StubNotepad.uid3000 \
 /org/qdistro/App1 \
 org.qdistro.App1.GetDocument') || {
    echo "FAIL: GetDocument query failed"; exit 1;
  }
  printf '%s\n' "$GETDOC" > "$ART/12-s4-getdocument-attempt-${attempt}.log"
  printf '%s\n' "$GETDOC" | grep -Fq '[text/plain] hello_visual' && break
  sleep 0.25
done
printf '%s\n' "$GETDOC" > "$ART/12-s4-getdocument.log"
NOTEPAD_PID_AFTER=$($VMEXEC "$VM" \
 'systemctl --machine=work2@.host --user show qstub-notepad.service -p MainPID --value')

SQL_AUDIT_B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh || exit 2
qci_sqlite audit "SELECT caller_uid, action, decision, scope, source, approver_uid FROM audit ORDER BY id DESC LIMIT 1;"
EOF
)
SQL_COUNT_B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh || exit 2
qci_sqlite approvals "SELECT count(*) FROM approvals WHERE action LIKE 'app.send-to:%';"
EOF
)
AUDIT_ROW=$($VMEXEC "$VM" "echo $SQL_AUDIT_B64 | base64 -d | bash")
APPROVAL_COUNT=$($VMEXEC "$VM" "echo $SQL_COUNT_B64 | base64 -d | bash")
printf '%s\n' "$AUDIT_ROW" > "$ART/12-s4-audit-row.txt"
printf '%s\n' "$APPROVAL_COUNT" > "$ART/12-s4-approval-count.txt"
printf '%s\n' "$NOTEPAD_PID_BEFORE" "$NOTEPAD_PID_AFTER" > "$ART/12-s4-notepad-pid.txt"
$VMEXEC "$VM" 'cat /tmp/12-relay.out 2>/dev/null || true' > "$ART/12-s4-sender.log"
```

**Assert**:
- `12-s4-getdocument.log` contains `[text/plain] hello_visual` within 10 seconds.
- `12-s4-notepad-pid.txt` contains the same nonzero PID on both lines.
- `12-s4-audit-row.txt` equals
 `2000|app.send-to:3000:org.qdistro.StubNotepad.uid3000|1|once|prompt|1000`.
- `12-s4-approval-count.txt` is `0` — one_shot actions never cache.

### S5 — two real qnotebooks, one per silo, are discoverable receivers

In the guest driver, after the host released the S4 token:

```bash
# Fresh, owned notebook directories; each qnotebook runs offscreen as its
# silo user on that user's own session bus (the s125-sendto-xuser.sh pattern).
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh || exit 2
install -d -m 0700 /run/qci-pg12
for pair in work:2000 work2:3000; do
  u=${pair%%:*}; id=${pair##*:}
  home=$(getent passwd "$u" | cut -d: -f6)
  dir="$home/qci-pg12-nb"
  rm -rf "$dir"; install -d -o "$u" -m 0700 "$dir"
  runuser -u "$u" -- env XDG_RUNTIME_DIR=/run/user/$id \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$id/bus \
    QT_QPA_PLATFORM=offscreen PYTHONUNBUFFERED=1 \
    setsid -f /usr/local/bin/qnotebook "$dir" >"/run/qci-pg12/qnb-$u.log" 2>&1 </dev/null
done
await_dbus_session_name org.qdistro.Qnotebook.uid2000 work 60 || exit 1
await_dbus_session_name org.qdistro.Qnotebook.uid3000 work2 60 || exit 1
await_broker_receiver 2000 org.qdistro.Qnotebook.uid2000 30 || exit 1
await_broker_receiver 3000 org.qdistro.Qnotebook.uid3000 30 || exit 1
dbus-send --system --print-reply --reply-timeout=5000 \
  --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.ListReceivers > /run/qci-pg12/receivers.txt 2>&1
echo "qnb_pids=$(pgrep -u work -f '[q]notebook' | head -1) $(pgrep -u work2 -f '[q]notebook' | head -1)"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# Then, still in the driver:
#   qci_host_step s5-qnb-receivers
```

While the driver waits, the host copies the receiver list into the artifact
directory, then releases the token:

```bash
$VMEXEC "$VM" 'cat /run/qci-pg12/receivers.txt' > "$ART/12-s5-receivers.txt"
```

**Assert**:
- Both receiver waits passed, and `12-s5-receivers.txt` lists
  `org.qdistro.Qnotebook.uid2000` (uid 2000) AND
  `org.qdistro.Qnotebook.uid3000` (uid 3000): the broker discovers both real
  apps, not just the target.

### S6 — a RelayMessage from work to work2's qnotebook is pending in the app

In the guest driver:

```bash
B64=$(base64 -w0 <<'EOF'
set -e
runuser -u work -- dbus-send --system --print-reply --reply-timeout=120000 \
 --dest=org.qdistro.AdminBroker1 \
 /org/qdistro/AdminBroker1 \
 org.qdistro.AdminBroker1.RelayMessage \
 int32:3000 \
 string:org.qdistro.Qnotebook.uid3000 \
 string:text/plain \
 string:visual_phase4_hello \
 >/tmp/12-qnb-relay.out 2>&1 &
echo $! >/tmp/12-qnb-relay.pid
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh; await_broker_pending_action 'app.send-to:3000:org.qdistro.Qnotebook.uid3000' 30"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals \(1 pending\)" 30'
# Then, still in the driver:
#   qci_host_step s6-qnb-pending
```

While the driver waits, the host captures and inspects the frame, then
releases the gate:

```bash
qdwin_screenshot "$ART/12-s6-qnb-pending.png"
```

**Assert (vision, on `12-s6-qnb-pending.png`)** — substrings that must be
visible; match on the core words:
- `uid=2000` (detail pane header).
- `app.send-to:3000:org.qdistro.Qnotebook.uid3000` (action line).
- `kind=text/plain`, `payload=visual_phase4_hello`, `target_uid=3000`,
  `target_service=org.qdistro.Qnotebook.uid3000`.
- `Just this once` label with its radio in the filled state.
- Buttons labeled `Approve` and `Deny` are present.

### S7 — click Approve

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
# Runner: locate "Approve" in 12-s6-qnb-pending.png and click its centre with
# the preview / confirm handshake (AGENTS.md 3b):
#   $VMGUI "$VM" click-preview <cx> <cy> "Approve"
#   $VMGUI "$VM" click-confirm <preview-manifest>
# Then wait for the list to empty before the frame. A timeout FAILS S7.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'
qdwin_screenshot "$ART/12-s7-qnb-approved.png"
```

**Assert (vision, on `12-s7-qnb-approved.png`)**:
- `(no selection)` is visible again.
- No text starting with `uid=2000` or `app.send-to:` on screen.

### S8 — delivery to the real receiver + audit + no-cache

In the guest driver, after the S7 capture: `qci_host_step s8-qnb-delivery`.
While it waits (same discipline as S4: query, record, evaluate, THEN release
the exact token), the host runs:

```bash
QNB_PID_BEFORE=$($VMEXEC "$VM" "pgrep -u work2 -f '[q]notebook' | head -1")
LAST=
deadline=$((SECONDS + 10))
attempt=0
while [ "$SECONDS" -lt "$deadline" ]; do
  attempt=$((attempt + 1))
  LAST=$($VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply --reply-timeout=1000 \
 --dest=org.qdistro.Qnotebook.uid3000 \
 /org/qdistro/App1 org.qdistro.App1.GetLastReceived') || {
    echo "FAIL: GetLastReceived query failed"; exit 1;
  }
  printf '%s\n' "$LAST" > "$ART/12-s8-lastreceived-attempt-${attempt}.log"
  printf '%s\n' "$LAST" | grep -Fq '[text/plain] visual_phase4_hello' && break
  sleep 0.25
done
printf '%s\n' "$LAST" > "$ART/12-s8-lastreceived.log"
QNB_PID_AFTER=$($VMEXEC "$VM" "pgrep -u work2 -f '[q]notebook' | head -1")
SQL_AUDIT_B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh || exit 2
qci_sqlite audit "SELECT caller_uid, action, decision, scope, source, approver_uid FROM audit WHERE action='app.send-to:3000:org.qdistro.Qnotebook.uid3000' ORDER BY id DESC LIMIT 1;"
EOF
)
SQL_COUNT_B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh || exit 2
qci_sqlite approvals "SELECT count(*) FROM approvals WHERE action LIKE 'app.send-to:%';"
EOF
)
$VMEXEC "$VM" "echo $SQL_AUDIT_B64 | base64 -d | bash" > "$ART/12-s8-audit-row.txt"
$VMEXEC "$VM" "echo $SQL_COUNT_B64 | base64 -d | bash" > "$ART/12-s8-approval-count.txt"
printf '%s\n' "$QNB_PID_BEFORE" "$QNB_PID_AFTER" > "$ART/12-s8-qnotebook-pid.txt"
$VMEXEC "$VM" 'cat /tmp/12-qnb-relay.out 2>/dev/null || true' > "$ART/12-s8-sender.log"
```

**Assert**:
- `12-s8-lastreceived.log` contains `[text/plain] visual_phase4_hello` within
  10 seconds: the Approve click reached work2's real qnotebook. (`GetLastReceived`
  records arrival; it does not prove the editor inserted the text — neither
  did 16.)
- `12-s8-qnotebook-pid.txt` holds the same nonzero PID twice (no receiver
  restart in between).
- `12-s8-audit-row.txt` equals
  `2000|app.send-to:3000:org.qdistro.Qnotebook.uid3000|1|once|prompt|1000`.
- `12-s8-approval-count.txt` is `0` — one-shot actions never cache.

## Teardown

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user restart qstub-notepad.service'
$VMEXEC "$VM" 'pkill -u work -f "[q]notebook" 2>/dev/null; pkill -u work2 -f "[q]notebook" 2>/dev/null; true'
$VMEXEC "$VM" 'rm -rf /home/work/qci-pg12-nb /home/work2/qci-pg12-nb /run/qci-pg12; rm -f /tmp/12-relay.out /tmp/12-relay.pid /tmp/12-qnb-relay.out /tmp/12-qnb-relay.pid'
```

## Notes for the runner

- Driving qnotebook's own *sender* UI (Plugins → "Qdistro: Send selection
 to…") is not exercised; the plugin's MenuProvider is unit-tested.
- S2's `dbus-send` has a 25s default reply timeout. If you take
 longer than that between S2 and S3, the sender gets NoReply and
 its process exits — the broker has already recorded the pending,
 and the admin click still delivers the payload + audit row
 (the reply just goes nowhere). S4's assertions verify the
 broker and notepad state, not the sender's exit.
- Don't hard-code pixel coordinates for the Approve click. The
 admin app's layout shifts with Qt font/DPI/theme; locate the
 `Approve` text in the frame you just took.
