# 28 — Rule `exe` selector matches via fnmatch glob

**What**: install one rule that allows `test.action` for any caller
whose `exe` matches `/usr/bin/python*`. Run `qdistro-test-permission`
as `work` (caller exe is `/usr/bin/python3.13`); expect ALLOWED with
no prompt. Then trigger the same action from a different exe
(`/usr/bin/perl -e 'use Net::DBus; ...'`) and expect a pending row
(rule does NOT match perl).

**Why**: `permissions.md` §Declarative rules: "String selectors
(`action`, `exe`, `app_id`, `mime_type`, `sandbox_engine`) accept
fnmatch-style globs when the value contains `*`; exact match
otherwise." This is what makes the rule language ergonomic for the
common case ("any python under /usr/bin") without giving admins a
foot-gun (literal-string-without-`*` stays exact). A regression
that treated `python*` as a literal would deny the python caller;
a regression that treated `/usr/bin/python*` as `/usr/bin/*` would
match perl too.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f "perl.*RequestPermission" 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
DELETE FROM audit WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Steps

### S1 — install the python-only allow rule

```bash
B64=$(base64 -w0 <<'EOF'
YAML='- name: allow-python-test-action
  decision: allow
  match:
    uid: 2000
    action: test.action
    exe: /usr/bin/python*
  rationale: scenario 28 — exe glob match
'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.SaveRule \
  string:"28-allow-python.yaml" \
  string:"$YAML"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1
```

**Assert**: `SaveRule` reply is the full path of the new file.

### S2 — python caller is ALLOWED, no prompt

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
$VMGUI "$VM" screenshot /tmp/28-s2a-app-empty.png

B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
  >/tmp/28-work-py.log 2>&1 & echo $! >/tmp/28-work-py.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/28-s2b-app-stillempty.png
$VMEXEC "$VM" 'wait $(cat /tmp/28-work-py.pid) 2>/dev/null; cat /tmp/28-work-py.log'
```

**Assert**:
- `/tmp/28-s2a-app-empty.png` and `/tmp/28-s2b-app-stillempty.png`
  both show empty pending list — the python call did NOT enqueue
  a prompt.
- `/tmp/28-work-py.log` contains `ALLOWED`.

### S3 — `dbus-send` caller from `work` falls through to prompt

```bash
# Use /usr/bin/dbus-send as the alternate caller exe — already in
# the base distro, no extra packages. The broker captures
# /proc/$pid/exe at RequestPermission time, which will be
# /usr/bin/dbus-send — does NOT match the rule's `/usr/bin/python*`.
B64=$(base64 -w0 <<'EOF'
# Issue RequestPermission as work, capture the request id, then
# WaitForDecision in a second call (still as work). Long reply-
# timeout so admin's deny click has time to land.
sudo -u work bash -c '
  RID_OUT=$(dbus-send --system --print-reply --reply-timeout=5000 \
    --dest=org.qdistro.AdminBroker1 \
    /org/qdistro/AdminBroker1 \
    org.qdistro.AdminBroker1.RequestPermission \
    string:"test.action" \
    dict:string:string:"caller","dbus-send-28" 2>&1)
  echo "$RID_OUT" > /tmp/28-rid.out
  RID=$(echo "$RID_OUT" | awk "/int32/{print \$2; exit}")
  echo "rid=$RID" >> /tmp/28-rid.out
  dbus-send --system --print-reply --reply-timeout=60000 \
    --dest=org.qdistro.AdminBroker1 \
    /org/qdistro/AdminBroker1 \
    org.qdistro.AdminBroker1.WaitForDecision \
    int32:$RID > /tmp/28-work-alt.log 2>&1 &
  echo $! > /tmp/28-work-alt.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/28-s3-prompt-visible.png
$VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.GetPending'
```

**Assert** (`/tmp/28-s3-prompt-visible.png` + GetPending output):
- Admin app's pending list has exactly one entry: `uid=2000  test.action`.
- The detail pane (or the GetPending reply) shows the caller's
  `exe` = `/usr/bin/dbus-send`, NOT `/usr/bin/python*`.
- `caller=dbus-send-28` appears in the details line.

### S4 — admin denies the alt-exe request, side-effect check

```bash
# Click `Deny` via OCR, or use Ctrl+N once the admin window is focused.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 2
$VMGUI "$VM" screenshot /tmp/28-s4-after-deny.png
$VMEXEC "$VM" 'wait $(cat /tmp/28-work-alt.pid) 2>/dev/null; cat /tmp/28-work-alt.log'
```

**Assert**:
- `/tmp/28-s4-after-deny.png` shows the pending list empty.
- `/tmp/28-work-alt.log` contains the substring `boolean false`
  (dbus-send's rendering of `WaitForDecision`'s `False` reply for
  a denied request). A `org.qdistro.AdminBroker1.Denied` error
  name is also acceptable if the broker raises instead of
  returning false — both reflect the deny on the wire.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f "dbus-send.*WaitForDecision" 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/28-rid.out /tmp/28-work-alt.log /tmp/28-work-alt.pid /tmp/28-work-py.log /tmp/28-work-py.pid'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
DELETE FROM audit WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
$VMEXEC "$VM" 'rm -f /tmp/28-work-*.log /tmp/28-work-*.pid'
```

## Notes for the runner

- The load-bearing property of S3 is that the caller's `exe`
  captured by the broker is a **non-python** path. `dbus-send`
  (`/usr/bin/dbus-send`, base distro) is chosen because it
  ships in baseweed. If S3 shows the exe field as `python` after
  all, vm-exec's `sudo -u work bash -c '...'` is somehow re-
  invoking python for the dbus-send call — check the captured
  `/tmp/28-rid.out`.
- `WaitForDecision` returns a boolean: `dbus-send` renders true /
  false as `boolean true` / `boolean false`. Older broker
  builds raised `org.qdistro.AdminBroker1.Denied` on the deny
  path instead; either form satisfies the assert.
