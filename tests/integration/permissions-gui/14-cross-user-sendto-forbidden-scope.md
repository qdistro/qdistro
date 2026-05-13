# 14 — cross-user send-to, admin picks non-once scope → ScopeNotPermitted

**What**: admin receives a RelayMessage, picks `1 hour` via the
scope radio group, clicks **Approve**. Broker rejects with
`ScopeNotPermitted`; admin app surfaces this in a modal
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
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user restart qstub-notepad.service'
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
```

## Steps

### S1 — trigger RelayMessage

```bash
B64=$(base64 -w0 <<'EOF'
set -e
runuser -u work -- dbus-send --system --print-reply \
 --dest=com.qdistro.AdminBroker1 \
 /com/qdistro/AdminBroker1 \
 com.qdistro.AdminBroker1.RelayMessage \
 int32:3000 \
 string:com.qdistro.StubNotepad.uid3000 \
 string:text/plain \
 string:scope_test_payload \
 >/tmp/14-relay.out 2>&1 &
echo $! >/tmp/14-relay.pid
sleep 1
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/14-s1-pending.png
```

**Assert (OCR /tmp/14-s1-pending.png)**:
- `uid=2000` and `app.send-to:3000:com.qdistro.StubNotepad.uid3000` visible.
- `payload=scope_test_payload` visible.
- Scope labels `Just this once`, `1 hour`, `24 hours`, `Forever`
 all visible (default radio is `Just this once`).

### S2 — select "1 hour" via OCR targeting

```bash
# Runner:
# 1. OCR /tmp/14-s1-pending.png.
# 2. Find the bounding box of the visible text "1 hour".
# 3. The clickable radio is immediately to the LEFT of that
# label; click a point ~15px left of the label's left
# edge at the label's vertical center.
$VMGUI "$VM" click <cx> <cy>
sleep 0.5
$VMGUI "$VM" screenshot /tmp/14-s2-1hour-selected.png
```

**Assert (OCR /tmp/14-s2-1hour-selected.png)**:
- `1 hour` still visible (just confirming no layout collapse).
- Radio-state verification is optional at the OCR level — if OCR
 can't reliably read the filled-vs-unfilled glyph, pass on text
 alone; S3 will pin the state indirectly (only a non-once scope
 can trigger ScopeNotPermitted).

### S3 — click Approve, expect ScopeNotPermitted modal

```bash
# Runner: OCR again and click the "Approve" button.
sleep 1
$VMGUI "$VM" screenshot /tmp/14-s3-rejected.png
```

**Assert (OCR /tmp/14-s3-rejected.png)**:
- A modal with heading text `Decision not recorded` has appeared.
- The modal body contains the substring
 `ScopeNotPermitted`.
- The modal body contains the substring
 `scope '1h' not permitted for one-shot`.
- Behind the modal (it may be greyed out), the pending list row
 (`uid=2000 app.send-to:3000:...`) is still visible — the
 request was NOT decided; admin gets another chance.
- The modal has a button labeled `OK` (or similar dismiss).

### S4 — dismiss modal, retry with "Just this once"

```bash
# Runner:
# 1. OCR /tmp/14-s3-rejected.png, find "OK" button, click it.
# 2. OCR the next screenshot, find "Just this once", click the
# radio ~15px left of that label.
# 3. OCR again, find "Approve", click it.
sleep 2
$VMGUI "$VM" screenshot /tmp/14-s4-once-approved.png
```

**Assert (OCR /tmp/14-s4-once-approved.png)**:
- `(no selection)` visible.
- No modal on screen.
- No `uid=2000` / `app.send-to:` text remaining.

```bash
# Side-effect assertions.
$VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply \
 --dest=com.qdistro.StubNotepad.uid3000 \
 /com/qdistro/App1 \
 com.qdistro.App1.GetDocument'

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
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/14-relay.out /tmp/14-relay.pid'
```

## Notes for the runner

- S3's modal is the critical assertion: it's the only place an
 admin learns that the broker refused their scope choice. If the
 modal doesn't appear, report FAIL before attempting S4 — the
 admin app's `on_decide_failed` handler (or equivalent) is
 broken and the whole scope-enforcement UX collapses.
- S2's assertion on radio-filled-state is intentionally soft.
 OCR glyph reading for radio bullets is unreliable; rely on S3's
 ScopeNotPermitted to prove that the broker saw a `1h` scope,
 which can only happen if the radio actually flipped.
