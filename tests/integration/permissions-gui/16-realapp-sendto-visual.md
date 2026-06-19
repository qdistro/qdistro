# 16 — cross-user send-to between two real qnotebook instances, visual admin-app approve

**What**: run the scenario-15 flow on the live admin compositor display with
admin's approvals GUI in the middle. Launch qnotebook as `work`
and `work2` via the xhost SI allowlist helper, trigger a
`RelayMessage` as work in the background, OCR-verify the admin
approval dialog, click **Approve**, verify the payload landed in
work2's qnotebook via `GetLastReceived`.

**Why**: 15 covered the wire; 16 proves the admin GUI surface
carries the real-app action strings (`app.send-to:3000:
org.qdistro.Qnotebook.uid3000`) and the approve click reaches the
plugin's receive code on the target uid.

**Caveat (loud)**: relies on the shared-XWayland
expedient per . Any silo user
can XTest any other — not the production model.

** scope note**: qterminator side deferred per
. Both qnotebook instances
prove the plugin architecture works identically for any
participant.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1957}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

# Ensure both user-relays are up with linger. Without these the broker
# cannot discover cross-silo receivers, so ListReceivers (S2) and
# GetLastReceived (S5) come back empty. Mirrors scenario 15's setup.
$VMEXEC "$VM" 'loginctl enable-linger work work2'
$VMEXEC "$VM" 'systemctl start user@2000.service user@3000.service'
for _ in 1 2 3 4 5; do
 $VMEXEC "$VM" 'test -S /run/user/2000/bus && test -S /run/user/3000/bus' && break
 sleep 1
done
$VMEXEC "$VM" 'systemctl --machine=work@.host --user restart qdistro-user-relay.service'
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user restart qdistro-user-relay.service'

$VMEXEC "$VM" 'systemctl --machine=work@.host --user stop qstub-notepad.service 2>/dev/null || true'
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user stop qstub-notepad.service 2>/dev/null || true'

SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action LIKE 'app.send-to:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 2>/dev/null; true"

# Launch both qnotebook instances on admin's compositor via the helper.
$VMEXEC "$VM" '
 pkill -u work -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 pkill -u work2 -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 sleep 1
 rm -f /home/work/testnb/.zim-qt/lock /home/work2/testnb/.zim-qt/lock
 /usr/local/bin/qdistro-start-user-app work /usr/local/bin/qnotebook /home/work/testnb
 /usr/local/bin/qdistro-start-user-app work2 /usr/local/bin/qnotebook /home/work2/testnb
'
sleep 6

# Start and raise the admin app after qnotebook so approval UI is not
# hidden behind the notebook windows. Keep the qnotebooks visible at
# the bottom of the display for the S1 visual smoke, but leave the
# admin app in front for OCR and the Approve click.
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 2
$VMEXEC "$VM" 'runuser -u admin -- env DISPLAY=:0 sh -c '"'"'
  i=0
  for w in $(xdotool search --onlyvisible --name ".*qnotebook.*" 2>/dev/null || true); do
    if [ "$i" -eq 0 ]; then xdotool windowsize "$w" 420 170 windowmove "$w" 20 610
    else xdotool windowsize "$w" 420 170 windowmove "$w" 840 610
    fi
    i=$((i + 1))
  done
  w=$(xdotool search --onlyvisible --name ".*admin approvals.*" 2>/dev/null | head -1 || true)
  if [ -n "$w" ]; then
    xdotool windowsize "$w" 900 560 windowmove "$w" 190 70
    xdotool windowactivate --sync "$w" windowraise "$w"
  fi
'"'"''
```

## Steps

### S1 — all three windows on screen

```bash
$VMGUI "$VM" activate "admin approvals"
$VMGUI "$VM" screenshot /tmp/16-s1-layout.png
```

**Assert (OCR /tmp/16-s1-layout.png)**:
- `admin approvals` text visible (admin app titlebar).
- Some qnotebook/editor chrome visible, for example `Backlinks`,
 `NewPage`, `Bold`, `Italic`, or seeded page text such as `Home`,
 `Payloads`, `Test notebook` — at least one qnotebook window is
 rendering.

Layout is not prescribed. If a qnotebook instance collapsed
without drawing (e.g. compositor didn't map both), S2 still
runs; S1 just confirms the live-display setup worked. A single
qnotebook visible is enough to pass S1 if both services appear
on the bus in S2.

### S2 — receivers discoverable via broker

```bash
$VMEXEC "$VM" 'dbus-send --system --print-reply \
 --dest=org.qdistro.AdminBroker1 \
 /org/qdistro/AdminBroker1 \
 org.qdistro.AdminBroker1.ListReceivers'
```

**Assert**:
- `org.qdistro.Qnotebook.uid2000` present.
- `org.qdistro.Qnotebook.uid3000` present.

### S3 — trigger a RelayMessage as work

```bash
B64=$(base64 -w0 <<'EOF'
set -e
runuser -u work -- dbus-send --system --print-reply \
 --dest=org.qdistro.AdminBroker1 \
 /org/qdistro/AdminBroker1 \
 org.qdistro.AdminBroker1.RelayMessage \
 int32:3000 \
 string:org.qdistro.Qnotebook.uid3000 \
 string:text/plain \
 string:visual_phase4_hello \
 >/tmp/16-relay.out 2>&1 &
echo $! >/tmp/16-relay.pid
sleep 1
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" activate "admin approvals"
$VMGUI "$VM" screenshot /tmp/16-s3-pending.png
```

**Assert (OCR /tmp/16-s3-pending.png)**:
- `uid=2000` in the detail pane header.
- The action line is visibly an `app.send-to` request for qnotebook.
 OCR commonly reads `to` as `t0` and `org` as `0rg`, so do not require
 the full exact action string from OCR. The exact action is asserted by
 the S6 audit row.
- `kind=text/plain`, recognizable `visual_phase4_hello` payload text,
 `target_uid=3000`, and recognizable qnotebook target service text are
 present. OCR may wrap or confuse punctuation (`=` as `-`, `org` as
 `0rg`), so accept stable fragments rather than exact `key=value`
 strings. S2 and S6 assert the exact DBus service and action.
- `Just this once` scope label with radio in filled state (or, if
 OCR can't reliably detect the glyph, `Just this once`,
 `hour`, `24 hours`, and at least one `Forever` label present in order).
- `Approve` and `Deny` buttons present.

### S4 — click Approve via OCR targeting

```bash
# Runner:
# 1. OCR /tmp/16-s3-pending.png.
# 2. Find bounding box of the visible text "Approve".
# 3. Click its center:
# $VMGUI "$VM" click <cx> <cy>
sleep 2
$VMGUI "$VM" screenshot /tmp/16-s4-approved.png
```

**Assert (OCR /tmp/16-s4-approved.png)**:
- `(no selection)` visible in detail pane.
- No `uid=2000` / `app.send-to:` text.

### S5 — work2's qnotebook saw the text

```bash
$VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply \
 --dest=org.qdistro.Qnotebook.uid3000 \
 /org/qdistro/App1 \
 org.qdistro.App1.GetLastReceived'
```

**Assert**: output contains `"[text/plain] visual_phase4_hello"`.

### S6 — audit + no-cache

```bash
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
- Audit row: `2000|app.send-to:3000:org.qdistro.Qnotebook.uid3000|1|once|prompt|1000`.
- Approvals-row count: `0`.

## Teardown

```bash
$VMEXEC "$VM" '
 pkill -u admin -f qdistro_admin_app 2>/dev/null || true
 pkill -u work -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 pkill -u work2 -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 rm -f /tmp/16-*.out /tmp/16-*.pid
'
```

## Notes for the runner

- Three windows on one compositor session stack — if the admin app is
 buried behind a qnotebook, OCR for `admin approvals` and click
 its window first to raise it, then retake the S3 screenshot.
- Driving qnotebook's own *sender* UI (selecting text + right-
 click → Plugins → qdistro send-to… → pick target) is
 intentionally NOT exercised in this scenario: rich-editor
 context menus across XWayland via OCR is fragile.
 Scenario 15 covers the wire path; the plugin's MenuProvider
 code is unit-tested. Manual smoke-test: open qnotebook by hand,
 select a paragraph, Menu bar → Plugins → "Qdistro: Send
 selection to…" → pick the peer.
- Editor-visible appearance of the received text depends on
 qnotebook's qdoc→markdown round-tripping and may not match the
 wire payload byte-for-byte. GetLastReceived is the
 authoritative signal; don't add an OCR assertion for the
 received text inside the editor pane.
