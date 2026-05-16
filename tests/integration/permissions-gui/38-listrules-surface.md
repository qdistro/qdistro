# 38 — `ListRules` surfaces every loaded rule's fields

**What**: install three rules with distinct shapes (uid + action +
exe; action-only; action + glob exe), call `ListRules` as admin,
verify every saved field surfaces in the reply with the expected
sentinel for "don't care" (uid=-1, action="", exe="").

**Why**: there's no admin-app UI for rules yet (admin reads the
YAML directly), but tooling — CLI, future Rules tab, mobile admin
— calls `ListRules` to inspect the live rule set. The "don't care"
sentinels are non-obvious (D-Bus has no nullable primitives) and
trivial to regress. This is the wire-level shape test.

This is a headless scenario.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1
```

## Steps

### S1 — install three rules with distinct shapes

```bash
B64=$(base64 -w0 <<'EOF'
cat >/etc/qdistro/rules.d/38a-narrow.yaml <<'YAML'
- name: narrow-py
  decision: allow
  match:
    uid: 2000
    action: scenario38.narrow
    exe: /usr/bin/python3.13
  rationale: narrow rule — all selectors set
YAML
cat >/etc/qdistro/rules.d/38b-action-only.yaml <<'YAML'
- name: action-only
  decision: deny
  match:
    action: scenario38.action-only
  rationale: action-only — uid/exe are wildcards
YAML
cat >/etc/qdistro/rules.d/38c-glob.yaml <<'YAML'
- name: glob-python
  decision: allow
  match:
    action: scenario38.glob
    exe: /usr/bin/python*
  rationale: glob exe — uid is wildcard
YAML
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.ReloadRules'
```

**Assert**: ReloadRules reply contains `int32 3` and an empty error
array.

### S2 — `ListRules` returns three dicts with correct fields

```bash
$VMEXEC "$VM" 'runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.ListRules' \
  | tee /tmp/38-listrules.out
```

**Assert** (textual analysis of `/tmp/38-listrules.out`):
- Three dict entries in the outer array.
- One entry has `name="narrow-py"`, `decision="allow"`,
  `action="scenario38.narrow"`, `exe="/usr/bin/python3.13"`,
  `uid=int32 2000`.
- One entry has `name="action-only"`, `decision="deny"`,
  `action="scenario38.action-only"`, `exe=""` (empty string —
  "don't care"), `uid=int32 -1` (sentinel — "don't care").
- One entry has `name="glob-python"`, `decision="allow"`,
  `action="scenario38.glob"`, `exe="/usr/bin/python*"`,
  `uid=int32 -1`.
- Each entry's `source_path` ends in the expected filename
  (`38a-narrow.yaml`, `38b-action-only.yaml`, `38c-glob.yaml`).
- Each entry's `rationale` matches the YAML.

### S3 — non-admin cannot call `ListRules`

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u work -- dbus-send --system --print-reply --reply-timeout=2000 \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.ListRules 2>&1 | tee /tmp/38-listrules-work.out
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: `/tmp/38-listrules-work.out` contains
`Error com.qdistro.AdminBroker1.AccessDenied` (broker-side
admin-only check; the system-bus policy may reject the call
even earlier with `org.freedesktop.DBus.Error.AccessDenied`,
which is also acceptable — log whichever appears).

## Teardown

```bash
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml /tmp/38-*.out'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
```

## Notes for the runner

- D-Bus has no `null` for primitives; the rules engine uses
  `uid=-1` / `exe=""` / `action=""` / `scope=""` as the sentinel
  for "don't care". A regression that emitted `uid=0` instead
  would silently match root's actions only, surprising admin.
- S3 documents the policy gate but admits either error name — the
  load-bearing property is that a non-admin doesn't get back a
  rule list. If the call *succeeds* for `work`, that's a real
  finding; the admin's rule set has historically leaked through
  bus-policy gaps.
