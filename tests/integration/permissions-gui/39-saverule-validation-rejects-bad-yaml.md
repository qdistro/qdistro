# 39 — `SaveRule` rejects bad YAML / bad filename, no partial write

**What**: call `SaveRule` with (a) a filename containing a path
separator → `.RulesEngineRefused`; (b) a syntactically invalid
YAML body → `.RulesEngineRefused`; (c) a valid YAML body whose
*shape* doesn't match the rules schema → `.RulesEngineRefused`.
Verify each case raises the correct error, no file was written
to `/etc/qdistro/rules.d/`, and `ListRules` count is unchanged.

**Why**: `SaveRule` runs as root inside the broker — every byte
admin writes through it lands in a privileged path. The validation
is the only thing between admin-app input and the rule engine.
Three failure modes pin the load-bearing checks:
- filename traversal protection (`re.fullmatch r"[A-Za-z0-9_-]+\.yaml"`).
- YAML parse failure.
- Schema rejection by the rules engine's dry-run load.

This is a headless scenario.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

# Capture the pre-scenario rule count for the unchanged-count check.
$VMEXEC "$VM" 'runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.ListRules' \
  | grep -c '"name"' > /tmp/39-pre-count.txt
PRE_COUNT=$(cat /tmp/39-pre-count.txt)
echo "pre-count = $PRE_COUNT"
```

## Steps

### S1 — filename with `/` is refused

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply --reply-timeout=2000 \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.SaveRule \
  string:"../39-traversal.yaml" \
  string:"- name: x\n  decision: allow\n  match:\n    action: x\n" \
  2>&1 | tee /tmp/39-s1.out
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**:
- `/tmp/39-s1.out` contains `Error org.qdistro.AdminBroker1.RulesEngineRefused`.
- No file `../39-traversal.yaml` exists anywhere:
  ```bash
  $VMEXEC "$VM" 'find /etc/qdistro -name "*traversal*" 2>/dev/null; \
                 find / -name "39-traversal.yaml" 2>/dev/null'
  ```
  Output is empty.

### S2 — syntactically invalid YAML is refused

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply --reply-timeout=2000 \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.SaveRule \
  string:"39-bad-yaml.yaml" \
  string:"- name: x\n  decision: allow\n  match: {action: : [unbalanced" \
  2>&1 | tee /tmp/39-s2.out
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'ls -l /etc/qdistro/rules.d/ | grep 39 || echo NO-FILE'
```

**Assert**:
- `/tmp/39-s2.out` contains `Error org.qdistro.AdminBroker1.RulesEngineRefused`.
- The grep prints `NO-FILE` — no `39-bad-yaml.yaml` landed on
  disk. (Validation happens in a tempdir; the atomic rename
  never runs.)

### S3 — schema-mismatch YAML is refused

```bash
# Top-level is a dict, not a list — RulesEngine expects a list.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply --reply-timeout=2000 \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.SaveRule \
  string:"39-bad-shape.yaml" \
  string:"name: x\ndecision: allow\nmatch:\n  action: x\n" \
  2>&1 | tee /tmp/39-s3.out
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'ls -l /etc/qdistro/rules.d/ | grep 39 || echo NO-FILE'
```

**Assert**:
- `/tmp/39-s3.out` contains `RulesEngineRefused`.
- The grep prints `NO-FILE`.

### S4 — rule count and `ListRules` output unchanged

```bash
$VMEXEC "$VM" 'runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.ListRules' \
  | grep -c '"name"' > /tmp/39-post-count.txt
diff /tmp/39-pre-count.txt /tmp/39-post-count.txt
```

**Assert**: `diff` exits 0 — the rule count is identical to S0.

## Teardown

```bash
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml /tmp/39-*.out /tmp/39-*-count.txt'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
```

## Notes for the runner

- The S1 traversal test exploits a hypothetical regression where
  the filename regex is loosened to accept slashes. Today's regex
  `[A-Za-z0-9_-]+\.yaml` rejects it before any file system touch.
- The error name `RulesEngineRefused` is also raised on a perfectly
  valid YAML that fails the rules-engine's own schema check (S3).
  If a future patch splits these into two error names, update the
  asserts accordingly — but losing distinguishability between
  "bad filename" and "bad content" would reduce admin-app
  diagnostics quality.
