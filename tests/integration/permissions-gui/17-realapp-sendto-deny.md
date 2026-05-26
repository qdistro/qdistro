# 17 — cross-user send-to between real qnotebook instances, admin denies

**What**: same flow as 16 (work's qnotebook → work2's qnotebook
via broker, admin app in admin's labwc session), but admin clicks
**Deny**. Assert that work2's qnotebook receiver was NOT called
(its `GetLastReceived` reflects its pre-deny state), the sender
observes `Denied`, and the audit row records `decision=0`.

**Why**: 16 covers Approve. A silent downgrade to allow, or a
failure to suppress delivery on deny, would look identical on the
surface. Only the receiver state + audit distinguish genuine deny
from a false positive.

**Caveat (loud)**: shared-XWayland expedient. See
.

** scope note**: qterminator side deferred per
.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1957}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl --machine=work@.host --user stop qstub-notepad.service 2>/dev/null || true'
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user stop qstub-notepad.service 2>/dev/null || true'

SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action LIKE 'app.send-to:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 2>/dev/null; true"

# Launch qnotebook instances first so the admin app ends up in front.
$VMEXEC "$VM" '
 pkill -u work -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 pkill -u work2 -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 sleep 1
 rm -f /home/work/testnb/.zim-qt/lock /home/work2/testnb/.zim-qt/lock
 /usr/local/bin/qdistro-start-user-app work /usr/local/bin/qnotebook /home/work/testnb
 /usr/local/bin/qdistro-start-user-app work2 /usr/local/bin/qnotebook /home/work2/testnb
'
sleep 6

# Start admin app after qnotebooks (matches scenario 16's stable
# ordering) and wait for the window to become visible under
# labwc/XWayland before proceeding.
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 2
$VMEXEC "$VM" 'runuser -u admin -- env DISPLAY=:0 sh -c '"'"'
  for retry in 1 2 3 4 5 6; do
    w=$(xdotool search --onlyvisible --name ".*admin approvals.*" 2>/dev/null | head -1 || true)
    if [ -n "$w" ]; then
      xdotool windowactivate --sync "$w" windowraise "$w"
      break
    fi
    sleep 1
  done
'"'"''

# Snapshot pre-deny state of work2's qnotebook — anything already
# in GetLastReceived (from prior scenarios on this VM) is the
# baseline we assert doesn't change.
$VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply \
 --dest=org.qdistro.Qnotebook.uid3000 \
 /org/qdistro/App1 \
 org.qdistro.App1.GetLastReceived' > /tmp/17-baseline.out 2>&1
echo "=== pre-deny baseline ==="
cat /tmp/17-baseline.out
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
 string:org.qdistro.Qnotebook.uid3000 \
 string:text/plain \
 string:please_deny_me \
 >/tmp/17-relay.out 2>&1 &
echo $! >/tmp/17-relay.pid
sleep 1
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/17-s1-pending.png
```

**Assert (OCR /tmp/17-s1-pending.png)**:
- `uid=2000` visible.
- `payload=please_deny_me` on the details line.
- `Approve` and `Deny` buttons present.

### S2 — click Deny via OCR targeting

```bash
# Runner:
# 1. OCR /tmp/17-s1-pending.png.
# 2. Find bounding box of "Deny" (scope picker has no "Deny"
# label — only Approve/Deny buttons fit that width).
# 3. Click its center.
sleep 2
$VMGUI "$VM" screenshot /tmp/17-s2-denied.png
```

**Assert (OCR /tmp/17-s2-denied.png)**:
- `(no selection)` visible.
- No `uid=2000` / `app.send-to:` text.

### S3 — work2's qnotebook was NOT delivered to

```bash
$VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply \
 --dest=org.qdistro.Qnotebook.uid3000 \
 /org/qdistro/App1 \
 org.qdistro.App1.GetLastReceived' > /tmp/17-after.out 2>&1
cat /tmp/17-after.out

$VMEXEC "$VM" 'cat /tmp/17-relay.out'

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action, decision, scope, source FROM audit
 WHERE action LIKE 'app.send-to:%Qnotebook%'
 ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**:
- `/tmp/17-after.out` does NOT contain `please_deny_me`.
 (It may equal the baseline captured in Setup, or be empty if
 work2's qnotebook had no prior Receive this session.)
- `/tmp/17-relay.out` contains EITHER
 `org.qdistro.AdminBroker1.Denied` in its error-name line OR
 `org.freedesktop.DBus.Error.NoReply`. Both indicate the sender
 did not get a successful `method return`; the ground-truth
 suppression check is the GetLastReceived assertion above +
 audit decision=0 below. Why both are acceptable: the
 OCR-driven admin click can easily exceed dbus-send's 25s
 default reply timeout when three Qt apps and admin-app are
 all up on labwc; in that case dbus-send bails with NoReply
 even though the broker later delivers a `.Denied` that
 dbus-send is no longer listening for. What must NOT happen
 is a `method return` with no error — that would indicate
 success was reported to the caller.
- Audit row: `2000|app.send-to:3000:org.qdistro.Qnotebook.uid3000|0|once|prompt`.

## Teardown

```bash
$VMEXEC "$VM" '
 pkill -u admin -f qdistro_admin_app 2>/dev/null || true
 pkill -u work -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 pkill -u work2 -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 rm -f /tmp/17-*.out /tmp/17-*.pid
'
```

## Notes for the runner

- If S3 sees `please_deny_me` in `GetLastReceived`, that's a real
 regression — the broker relayed a denied message. Do NOT mask.
 Report the bug with the audit row, relay.out, and screenshot.
- If `/tmp/17-relay.out` does not contain `Denied` but
 GetLastReceived is clean, dbus-send likely timed out before
 admin clicked — re-run with the admin click happening within
 25 s.
