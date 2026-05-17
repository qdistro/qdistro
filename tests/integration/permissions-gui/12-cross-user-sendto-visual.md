# 12 — cross-user send-to, visual admin-app approve

**What**: trigger a RelayMessage from `work` in the background,
visually confirm the admin approvals app shows the request with
its full detail payload, click **Approve**, verify the payload
landed in `work2`'s notepad.

**Why**: proves the admin half of the thesis under a real UI —
the RelayMessage detail pane must render kind/payload/target_uid,
the "Just this once" radio is pre-selected (forbidden scopes are
broker-rejected), and the Approve click produces a cache-free
audit trail.

Sender-side GUI (qstub-sender as `work` on labwc XWayland) is
deferred — see .

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user restart qstub-notepad.service'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action LIKE 'app.send-to:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 2>/dev/null; true"
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
```

## Steps

### S1 — admin app up, pending empty

```bash
$VMGUI "$VM" screenshot /tmp/12-s1-empty.png
```

**Assert (OCR /tmp/12-s1-empty.png)**:
- Text `admin approvals` appears (window titlebar).
- Text `(no selection)` appears in the detail pane.
- Text `Pending` appears (tab label).
- No text starting with `uid=2000` or `app.send-to:` on screen.

### S2 — trigger a RelayMessage as work

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
 string:hello_visual \
 >/tmp/12-relay.out 2>&1 &
echo $! >/tmp/12-relay.pid
sleep 1
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/12-s2-pending.png
```

**Assert (OCR /tmp/12-s2-pending.png)** — every bullet below is
a substring that must be visible somewhere on screen. OCR
whitespace can be flaky, so match on the core words, not
character-perfect alignment:
- `uid=2000` (detail pane header).
- `app.send-to:3000:com.qdistro.StubNotepad.uid3000` (action line).
- `kind=text/plain`, `payload=hello_visual`, `target_uid=3000`,
 `target_service=com.qdistro.StubNotepad.uid3000` (all four keys
 in the details line — the broker detail sanitiser may join them
 with `,` and the font may wrap, but each key=value substring
 must be present).
- `Just this once` label with its radio in the filled state
 (scope picker default — if OCR can't reliably detect the radio
 glyph state, it's enough to verify the text is present and
 the other scope labels `1 hour` / `24 hours` / `Forever` appear
 after it).
- Buttons labeled `Approve` and `Deny` are present.

### S3 — click Approve via OCR targeting

```bash
# Runner:
# 1. OCR /tmp/12-s2-pending.png.
# 2. Find the bounding box of the visible text "Approve".
# There's exactly one such button in this window; if OCR
# returns multiple hits, pick the one closest to "Deny" (they
# sit next to each other at the bottom of the scope pane).
# 3. Click the center of that bounding box:
# $VMGUI "$VM" click <cx> <cy>
sleep 2
$VMGUI "$VM" screenshot /tmp/12-s3-approved.png
```

**Assert (OCR /tmp/12-s3-approved.png)**:
- `(no selection)` is visible again.
- No text starting with `uid=2000` or `app.send-to:` on screen.

### S4 — delivery + audit + no-cache

```bash
$VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply \
 --dest=com.qdistro.StubNotepad.uid3000 \
 /com/qdistro/App1 \
 com.qdistro.App1.GetDocument'

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
 `2000|app.send-to:3000:com.qdistro.StubNotepad.uid3000|1|once|prompt|1000`.
- Approvals-row count is `0` — one_shot actions never cache.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
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
 admin app's layout shifts with Qt font/DPI/theme; OCR targeting
 on the text `Approve` is what keeps this scenario portable.
