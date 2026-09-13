# 13 — cross-user send-to, admin denies

**What**: same RelayMessage as scenario 12, but admin clicks
**Deny**. Assert notepad never received the payload, sender sees
a `org.qdistro.AdminBroker1.Denied` DBusException, audit row
records decision=0.

**Why**: scenario 12 covers approve. A silent downgrade to allow
(or a failure to suppress delivery on deny) would look identical
in the UI to a genuine deny — only the notepad state + audit
distinguishes them.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user restart qstub-notepad.service'
# The suppression check below reads this stub's document. If the stub is not on
# the bus yet, GetDocument errors instead of answering, and "the output does not
# contain the payload" is then satisfied by a stub that was never reachable --
# a FALSE PASS on the one assertion that proves the deny actually worked.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && \
 await_dbus_session_name org.qdistro.StubNotepad.uid3000 work2 30 1'
# POSITIVE CONTROL. "Document lacks the payload" only proves suppression if
# this stub instance actually RECORDS deliveries; a stub that answers but
# silently drops every Receive would look identical. Plant a sentinel directly
# on work2's own session bus (no broker, no prompt, no cross-user hop) so S3
# can assert the document is present-but-different rather than merely a
# successful call.
$VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply --reply-timeout=5000 \
 --dest=org.qdistro.StubNotepad.uid3000 \
 /org/qdistro/App1 \
 org.qdistro.App1.Receive string:text/plain string:s13_sentinel' || {
 echo "ERROR: could not plant the s13 sentinel (see the dbus-send error above)."
 echo "Do not diagnose the cause from this line alone -- the owner may have"
 echo "exited, the object may be unexported, or policy/transport may have"
 echo "refused. What matters here: without the sentinel the suppression check"
 echo "below is unfalsifiable, so the scenario must not continue." >&2
 exit 1; }
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
```

## Steps

### S1 — trigger, confirm pending visible

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
 string:deny_me_please \
 >/tmp/13-relay.out 2>&1 &
echo $! >/tmp/13-relay.pid
sleep 1
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/13-s1-pending.png
```

**Assert (OCR /tmp/13-s1-pending.png)**:
- `uid=2000` visible.
- `payload=deny_me_please` visible in the details line.
- Buttons labeled `Approve` and `Deny` are present.

### S2 — deny via keyboard

```bash
# Mouse click delivery to Qt/XWayland windows is platform-blocked on
# this template. Drive the Deny action through the virtual keyboard
# path documented in AGENTS.md.
$VMEXEC "$VM" 'runuser -u admin -- env DISPLAY=:0 xdotool search --sync --name "admin approvals" windowactivate --sync'
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 2
$VMGUI "$VM" screenshot /tmp/13-s2-denied.png
```

**Assert (OCR /tmp/13-s2-denied.png)**:
- `(no selection)` visible again.
- No `uid=2000` / `app.send-to:` text on screen.

### S3 — side-effect assertions

```bash
# Notepad must NOT contain the payload.
$VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply \
 --dest=org.qdistro.StubNotepad.uid3000 \
 /org/qdistro/App1 \
 org.qdistro.App1.GetDocument' > "${QCI_SCENARIO_TMPDIR:-/tmp}/13-doc.out" 2>&1
cat "${QCI_SCENARIO_TMPDIR:-/tmp}/13-doc.out"

# Sender observed a Denied error.
$VMEXEC "$VM" 'cat /tmp/13-relay.out'

# Audit row exists with decision=0.
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action, decision, scope, source FROM audit
 WHERE action LIKE 'app.send-to:%' ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**:
- `${QCI_SCENARIO_TMPDIR:-/tmp}/13-doc.out` is a SUCCESSFUL reply — it
 contains `method return` — it CONTAINS the setup sentinel
 `[text/plain] s13_sentinel`, and it does NOT contain `deny_me_please`.
 All three are required. An error reply (`ServiceUnknown`, `NoReply`)
 trivially lacks the payload, so accepting one would let an unreachable
 notepad masquerade as a suppressed delivery; and a stub that answers
 but records nothing would too, which is what the sentinel rules out.
 If the reply is an error, or the sentinel is missing, the scenario is an
 ERROR, not a pass.
- `/tmp/13-relay.out` contains the substring
 `org.qdistro.AdminBroker1.Denied` in the error name. (A NoReply
 instead is acceptable only if the admin click came after
 dbus-send's 25s timeout — the notepad assertion above is the
 ground-truth suppression check.)
- Audit row: `2000|app.send-to:3000:...|0|once|prompt`.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/13-relay.out /tmp/13-relay.pid'
```
