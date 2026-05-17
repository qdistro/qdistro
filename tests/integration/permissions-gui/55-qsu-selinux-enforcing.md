# 55 — qsu end-to-end under SELinux Enforcing produces zero new AVCs

**What**: flip SELinux into Enforcing mode, run `qsu /bin/id` as
`work` (uid 2000), drive admin's approve via D-Bus (no GUI; the
admin app stays headless for this scenario), let the privileged
exec stream `id`'s output back, and assert that no new AVC
denials touching the qdistro-tier1 / qdistro-broker / qdistro-pwd
domains were logged in `audit.log` against the baseline cursor.

**Why**: every scenario 43-54 was authored against a permissive
VM (qd-sudo). The streamed `id` output of scenario 51 leaked
`unconfined_service_t` as the broker domain, confirming the qsu
code path has never been exercised under enforcing. Tier-1
SELinux is LIVE (commit `29178dd` "tier1: s50+s51 LIVE",
`f587d71` "s55 LIVE", `3d5d11d` "s56 broker-enforcing LIVE"),
which means a regression in `qdistro_tier1.te` or
`qdistro_broker.te` that breaks qsu would NOT be caught by the
existing tier1 bats sweep — `s55-tier1-enforcing.sh` exercises
`qdistro-tier1-exec` directly, not `/usr/local/bin/qsu`. This
scenario closes that gap.

This is a **headless** scenario AND it must be driven over SSH —
qemu-guest-agent's domain cannot `setenforce 1` (see "Transport"
below).

## Transport — IMPORTANT

`scripts/vm/vm-exec` shells through qemu-guest-agent, which runs
in a SELinux domain (`qemu_ga_t`-equivalent on Tumbleweed) whose
policy does NOT grant `selinux_setenforce`. Empirical evidence
(2026-05-16):

```bash
# Via qga / vm-exec:
$ vm-exec tier1-test 'setenforce 1'
/usr/sbin/setenforce: security_setenforce() failed: Permission denied
# Workarounds also denied:
$ vm-exec tier1-test 'systemd-run --scope -- setenforce 1'
Failed to start transient scope unit: Access denied
$ vm-exec tier1-test 'cat > /etc/systemd/system/foo.service ...'
bash: /etc/systemd/system/foo.service: Permission denied
```

The fix is to use SSH transport. Set:

```bash
export VM_SSH_PORT=<port>             # e.g. 5722
export VM_SSH_HOST=127.0.0.1
export VM_SSH_USER=root
export VM_SSH_KEY=$HOME/.ssh/qdistro_enforcing_id_ed25519
```

These are the same env vars `tests/integration/vm/helpers.bash`
uses. The bats wrapper for `phase7-tier1-enforcing` relies on
the SSH transport; this scenario reuses it.

Every command below that the runner executes against the VM is
written as `ssh "$VM_SSH_USER@$VM_SSH_HOST" -p "$VM_SSH_PORT" -i
"$VM_SSH_KEY" ...`. For brevity the rest of this file abbreviates
that whole prefix to `vm_ssh`. The runner should expand it to
the full ssh command in actual execution.

## Setup

```bash
VM=${VMNAME:-tier1-test-260516-1252}
# Layer qsu install + work user onto the tier1 VM. tier1-test
# baseline already has qsu installed (qsu.py in /usr/local/lib/
# qdistro/, root-exec socket up) but only the admin user is
# present. Create work uid 2000 if missing:
vm_ssh "id work 2>/dev/null || useradd -m -u 2000 -U work; loginctl enable-linger work"

# Pre-flight: confirm the kernel can actually flip to enforcing.
# If config-pinned permissive (/etc/selinux/config SELINUX=permissive),
# `setenforce 1` succeeds but `getenforce` stays Permissive — skip the
# scenario in that case.
vm_ssh '/usr/sbin/setenforce 1 && [ "$(/usr/sbin/getenforce)" = Enforcing ] || echo CONFIG_PINNED_PERMISSIVE'
vm_ssh '/usr/sbin/setenforce 0'

# Drain broker state.
vm_ssh '
  pkill -u work -f qsu 2>/dev/null || true
  rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml
  systemctl restart qdistro-admin-broker.service
  systemctl restart qdistro-root-exec.socket
  sleep 2
  sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE '"'"'qsu.exec:%'"'"';"
  sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE '"'"'qsu.exec:%'"'"';"
'
```

## Steps

### S1 — capture baseline audit cursor + flip to Enforcing

```bash
vm_ssh '
  BASELINE_TS=$(($(date +%s) - 1))
  echo $BASELINE_TS > /tmp/55-baseline-ts
  /usr/sbin/setenforce 1
  SE_MODE=$(/usr/sbin/getenforce)
  echo "MODE=$SE_MODE"
'
```

**Assert**: output contains `MODE=Enforcing`.

If the runner sees `MODE=Permissive`, this VM is config-pinned
to permissive (boot kernel arg or `/etc/selinux/config`) — report
SKIP, not FAIL, and explain. The bats `s55-tier1-enforcing.sh`
uses the same SKIP rule.

### S2 — qsu invocation under enforcing

```bash
vm_ssh '
  sudo -u work bash -c "/usr/local/bin/qsu /usr/bin/id \
    > /tmp/55-qsu.log 2>&1 & echo \$! > /tmp/55-qsu.pid"
  sleep 2
'
```

### S3 — admin approves via D-Bus (no GUI)

```bash
vm_ssh '
  runuser -u admin -- python3 - <<PYEOF
import dbus
bus = dbus.SystemBus()
obj = bus.get_object("com.qdistro.AdminBroker1",
                     "/com/qdistro/AdminBroker1")
iface = dbus.Interface(obj, "com.qdistro.AdminBroker1")
rows = [r for r in iface.GetPending() if str(r.get("action", "")).startswith("qsu.exec:")]
assert rows, "no qsu pending row found"
rid = int(rows[0]["id"])
iface.DecideRequest(rid, "allow", "forever_argv")
print("decided rid=", rid)
PYEOF
'
sleep 2
vm_ssh 'wait $(cat /tmp/55-qsu.pid) 2>/dev/null; head -3 /tmp/55-qsu.log'
```

**Assert**:
- The admin-side python prints `decided rid= <some int>` (no
  ScopeNotPermitted, no AccessDenied — confirms broker's own
  enforcing-domain reachability + the qdistro-broker SELinux
  module's allow rules cover its workload).
- `/tmp/55-qsu.log` contains `uid=2000(work) gid=2000(work)` —
  the privileged exec ran the target as the work uid, streaming
  output through qsu back to the caller. The SELinux context
  field in the `id` output will read `system_u:system_r:<type>:s0`
  where `<type>` may be `unconfined_service_t` (if the broker
  delegated without a domain transition) or a more specific
  qdistro type once the SELinux module is tightened. The
  load-bearing claim is the COMMAND RAN.

### S4 — collect new AVCs and check against the qsu code-path

```bash
vm_ssh '
  BASELINE_TS=$(cat /tmp/55-baseline-ts)
  # Give auditd a beat to flush.
  sleep 1
  ausearch -m AVC,USER_AVC \
    --start "$(date -d @"$BASELINE_TS" "+%x %T")" 2>/dev/null \
    | grep -E "scontext=[^ ]*:(qdistro_tier1_t|qdistro_broker_t|qdistro_pwd_t)" \
    > /tmp/55-avcs.txt || true
  wc -l < /tmp/55-avcs.txt
  echo "---"
  cat /tmp/55-avcs.txt
'
```

**Assert**:
- Line count is `0`. No new AVCs touching qdistro-tier1 /
  qdistro-broker / qdistro-pwd source contexts since baseline.
- If the count is non-zero, capture the AVCs in the report and
  attach `audit2allow -i /tmp/55-avcs.txt` output as a fix
  suggestion. That's the regression to file as a follow-up
  todo (and a candidate fix is a new allow rule in
  `qdistro_tier1.te` / `qdistro_broker.te`).

### S5 — restore permissive

```bash
vm_ssh '/usr/sbin/setenforce 0'
```

**Assert**: `getenforce` returns `Permissive`. (The Teardown
block also does this defensively.)

## Teardown

```bash
vm_ssh '
  /usr/sbin/setenforce 0 2>/dev/null || true
  pkill -u work -f qsu 2>/dev/null || true
  rm -f /tmp/55-*.log /tmp/55-*.pid /tmp/55-baseline-ts /tmp/55-avcs.txt
  sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE '"'"'qsu.exec:%'"'"';"
  sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE '"'"'qsu.exec:%'"'"';"
'
```

## Notes for the runner

- **Transport is SSH, NOT vm-exec.** qemu-guest-agent's domain
  cannot `setenforce 1`. If `VM_SSH_PORT` is not set in the
  environment, the runner must report ERROR with the specific
  message "qga transport insufficient for setenforce; this
  scenario requires VM_SSH_PORT". Do NOT try to work around it
  with `systemd-run --scope`, `/etc/systemd/system/foo.service`,
  or any other elevation gimmick — they all fail with `Access
  denied` from qga (validated 2026-05-16; see todo
  `qsu-selinux-enforcing-untested.md`).
- **tier1-test-260516-1252 may not have the work user.** The
  Setup step creates it if missing. If `useradd` fails (e.g.
  enforcing already on without a policy allow for user creation),
  re-run after `setenforce 0` is confirmed.
- **The qdistro-root-exec.service runs with
  `NoNewPrivileges=false`** by design — it has to escalate to
  arbitrary target users — so its SELinux domain has unusual
  capabilities. If S4 produces denials, scrutinise whether
  they're INSIDE the qdistro-root-exec workflow (between accept
  and execve) or in the spawned child after the setuid
  transition; the fix lives in different .te modules.
- **This scenario is the GUI-doc-form complement to the
  bats `s55-tier1-enforcing.sh` test.** s55 exercises
  `qdistro-tier1-exec` (tier-1 silo spawn) under enforcing; this
  scenario exercises `qdistro-root-exec` (qsu) under enforcing.
  Both can co-exist; neither subsumes the other.
- The audit cursor is captured as `(now - 1s)` to handle
  sub-second clock skew between the `date +%s` call and the
  actual auditd insertion. Matches the s55 pattern.
