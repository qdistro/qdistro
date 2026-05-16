# 50 — Admin rule `argv_prefix:` pre-approves qsu without a prompt

**What**: as admin, write a rule pre-approving any qsu invocation
whose argv starts with `[/usr/bin/systemctl, status]` (a read-only
verb). Then as `work`, run two qsu invocations:
1. `/usr/local/bin/qsu /usr/bin/systemctl status sshd` — argv
   prefix matches → rule fires, no admin prompt, audit row carries
   `source=rule` and `rule_path` points to the scenario's YAML file.
2. `/usr/local/bin/qsu /usr/bin/systemctl restart sshd` — argv
   prefix does NOT match (`restart` ≠ `status`) → request still
   pends, admin sees it in the app.

**Why**: `doc/sudo.md` §"Admin-authored policies (pre-approval)"
shows a YAML rule with `argv_prefix` as the canonical way to
pre-approve safe sudoable verbs without burning admin attention.
The rules-engine support is in `broker/qdistro_admin_rules.py`
(`argv_exact` / `argv_basename` / `argv_prefix` selectors, see
lines 127-138), validated only in unit tests against a stub
caller. This scenario pins the wire-level path: the broker
extracts argv from the qsu `details['argv[NN]']` keys, the rules
engine matches the prefix, and the audit ledger records the
rule's filename so admin can find it later.

## Setup

```bash
VM=${VMNAME:-qd-sudo}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

# Defensive: our own scenario-prefixed rule file. Re-clear in
# Teardown.
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/50-*.yaml /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl restart qdistro-root-exec.socket'
sleep 1

SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';
DELETE FROM audit WHERE action LIKE 'qsu.exec:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Steps

### S1 — install the argv-prefix allow rule

```bash
B64=$(base64 -w0 <<'EOF'
YAML='- name: allow-work-systemctl-status
  decision: allow
  match:
    uid: 2000
    action: qsu.exec:root
    argv_prefix: ["/usr/bin/systemctl", "status"]
  rationale: scenario 50 — argv_prefix pre-approval for read-only systemctl
'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.SaveRule \
  string:"50-allow-systemctl-status.yaml" \
  string:"$YAML"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1
```

**Assert**:
- `dbus-send` reply is
  `string "/etc/qdistro/rules.d/50-allow-systemctl-status.yaml"`.
- The file exists on disk:
  ```bash
  $VMEXEC "$VM" 'cat /etc/qdistro/rules.d/50-allow-systemctl-status.yaml'
  ```
  Output shows the YAML body verbatim (rules.d is inotify-watched;
  the broker has already loaded it).

### S2 — launch admin app; pending list must STAY empty

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
$VMGUI "$VM" screenshot /tmp/50-s2-empty.png
```

**Assert** (`/tmp/50-s2-empty.png`): pending list empty.

### S3 — qsu /usr/bin/systemctl status sshd → rule allow, no prompt

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /usr/bin/systemctl status sshd \
  >/tmp/50-status.log 2>&1 & echo $! >/tmp/50-status.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 3
$VMGUI "$VM" screenshot /tmp/50-s3-stillempty.png

$VMEXEC "$VM" 'wait $(cat /tmp/50-status.pid) 2>/dev/null; head -5 /tmp/50-status.log'
```

**Assert**:
- `/tmp/50-s3-stillempty.png`: admin app pending list still empty.
- `/tmp/50-status.log`: shows `systemctl status sshd` output
  (a line containing `sshd.service` and one of `active`,
  `inactive`, or `Loaded:`). The exit code is whatever systemctl
  decided; the load-bearing point is the command actually executed
  (rule allowed → exec happened → stdout streamed back).

### S4 — audit row carries `source=rule` and `rule_path`

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT decision, scope, source, rule_path FROM audit
  WHERE action='qsu.exec:root'
  ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: output is exactly
`1||rule|/etc/qdistro/rules.d/50-allow-systemctl-status.yaml`.
- `decision=1` (allow).
- `scope` empty (rules don't write cache rows; no scope was picked).
- `source=rule`.
- `rule_path` is the absolute path the SaveRule call wrote.

### S5 — qsu /usr/bin/systemctl restart sshd → DOES prompt

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /usr/bin/systemctl restart sshd \
  >/tmp/50-restart.log 2>&1 & echo $! >/tmp/50-restart.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/50-s5-pending.png
```

**Assert** (`/tmp/50-s5-pending.png`):
- One pending row visible.
- Action `qsu.exec:root`, uid 2000.
- Details show `argv=/usr/bin/systemctl restart sshd` — the rule
  did NOT match because `argv_prefix:["systemctl", "status"]`
  is not a prefix of `[systemctl, restart, sshd]`.

### S6 — admin denies; sender sees DENIED, audit shows scoped reject

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 2
$VMEXEC "$VM" 'wait $(cat /tmp/50-restart.pid) 2>/dev/null; cat /tmp/50-restart.log'
```

**Assert** (acceptable: either A or B — the load-bearing audit
+ pending-list assertions below hold either way):
- A (fast S6): `/tmp/50-restart.log` contains `request denied`
  (qsu's deny message) and qsu rc=1.
- B (slow S6, runner > ~22s between pending-row appearance and
  Ctrl+N): `/tmp/50-restart.log` contains
  `org.freedesktop.DBus.Error.NoReply` — qsu's
  RequestPermissionAs reply timeout fired before the broker's
  deny reply arrived. The broker still recorded the deny
  (pending list empties, audit row `0|prompt` lands). Pre-fix
  workaround: redeploy with the bumped timeouts in
  `qsu/qdistro_root_exec.py` (`timeout=90` on
  RequestPermissionAs, `timeout=900` on WaitForDecision).
  Tracked in todo broker-serialization-concurrent-qsu §3.
- Final audit:
  ```bash
  SQL_B64=$(base64 -w0 <<'SQL_EOF'
  SELECT decision, source FROM audit
    WHERE action='qsu.exec:root'
    ORDER BY id DESC LIMIT 2;
  SQL_EOF
  )
  $VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
  ```
  Two newest rows (reverse chrono):
  - `0|prompt` (the restart deny just landed).
  - `1|rule` (the status allow from S4 still there).

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/50-*.log /tmp/50-*.pid'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/50-*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';
DELETE FROM audit WHERE action LIKE 'qsu.exec:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- `argv_prefix` is a **list-equality** match on `argv[:len(prefix)]`,
  NOT a string-prefix match on `shlex.join(argv)`. So a rule with
  `argv_prefix: ["/usr/bin/systemctl"]` matches both
  `systemctl status` AND `systemctl restart` — because the prefix
  is length 1 and argv[0] matches. The scenario uses a length-2
  prefix `[systemctl, status]` to demonstrate selectivity.
- The broker reads argv per-element from `details['argv[NN]']`
  (lossless), not from `details['argv']` (shlex-joined display
  string). That's why a rule with a verb containing whitespace
  in S1's YAML matches reliably.
- If S4 returns `source=cache` instead of `source=rule`, the cache
  was not drained between scenarios (rules don't write cache rows
  by design; a stale row from a prior run leaks in). Re-run after
  the orchestrator drain.
- If S5 admin app shows NO pending row, the rule matched the
  restart argv too — that's a real bug in
  `_match_selectors_for_request` and the scenario is doing its job
  by flagging it. Capture the rule + screenshot and report FAIL.
