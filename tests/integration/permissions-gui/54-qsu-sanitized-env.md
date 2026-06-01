# 54 — qsu strips LD_*/PYTHONPATH and resets PATH before exec

**What**: as `work`, set `LD_PRELOAD=/tmp/evil.so`,
`PYTHONPATH=/tmp/poison`, and a hostile `PATH=/tmp/evilbin:/bin`,
then run `qsu /usr/bin/env`. Admin approves `forever_argv` for
this argv. The streamed-back env should show:
- `LD_PRELOAD` absent.
- `PYTHONPATH` absent.
- `PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`
  (the fixed safe value from `qdistro_root_exec._spawn_and_stream`).
- `USER=root`, `LOGNAME=root`, `HOME=/root`, `TERM=xterm` (the
  service's hardcoded baseline; the target user defaults to root).

**Why**: `doc/sudo.md` §"Sanitized environment" promises sudo-
equivalent `env_reset` so a sandboxed user can't smuggle
`LD_PRELOAD` past escalation and exploit the privileged process.
The `qdistro_root_exec._spawn_and_stream` `env = {…}` dict is the
load-bearing barrier — Popen is called with `env=env` (not
`os.environ.copy()`), so the privileged child sees ONLY those
keys. A regression that inherits the caller's env would let any
work-user write code into `/tmp/evil.so` and execute it as root
via any approved qsu call.

This is a headless+admin-app scenario.

## Setup

```bash
VM=${VMNAME:-qd-sudo}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl restart qdistro-root-exec.socket'
sleep 1

B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
```

## Steps

### S1 — work invokes qsu with poisoned env

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '
  export LD_PRELOAD=/tmp/evil.so
  export PYTHONPATH=/tmp/poison
  export LD_LIBRARY_PATH=/tmp/lib-evil
  export PATH=/tmp/evilbin:/bin
  setsid /usr/local/bin/qsu /usr/bin/env \
    >/tmp/54-env.log 2>/tmp/54-qsu-client.err </dev/null &
  echo $! >/tmp/54-env.pid
'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/54-s1-pending.png
```

**Assert** (`/tmp/54-s1-pending.png`): pending row visible; details
show `argv=/usr/bin/env`.

### S2 — admin approves once

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 2

$VMEXEC "$VM" 'wait $(cat /tmp/54-env.pid) 2>/dev/null
sort /tmp/54-env.log'
```

**Assert**: `/tmp/54-env.log` is the privileged command's stdout
only. qsu/client stderr is captured separately in
`/tmp/54-qsu-client.err`, because a hostile `LD_PRELOAD` can make
the dynamic loader warn before the qsu client reaches its own
sanitization boundary. `/tmp/54-env.log` (sorted) MUST contain:

```
HOME=/root
LOGNAME=root
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
TERM=xterm
USER=root
```

And MUST NOT contain any of:
- `LD_PRELOAD=`
- `LD_LIBRARY_PATH=`
- `PYTHONPATH=`
- `PATH=/tmp/evilbin` (or any PATH variant starting with `/tmp/`).

The grep-based runner check:

```bash
B64=$(base64 -w0 <<'EOF'
echo "--- present (should all match) ---"
grep -E '^(HOME=/root|LOGNAME=root|TERM=xterm|USER=root|PATH=/usr/local/sbin:.*)$' /tmp/54-env.log
echo "--- absent (each grep must return 0 matches) ---"
echo -n "LD_PRELOAD: "; grep -c '^LD_PRELOAD=' /tmp/54-env.log || echo 0
echo -n "LD_LIBRARY_PATH: "; grep -c '^LD_LIBRARY_PATH=' /tmp/54-env.log || echo 0
echo -n "PYTHONPATH: "; grep -c '^PYTHONPATH=' /tmp/54-env.log || echo 0
echo -n "tainted PATH: "; grep -c '^PATH=/tmp/' /tmp/54-env.log || echo 0
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**:
- Present section emits at least the 5 expected lines.
- Each "absent" count is exactly `0`.

### S3 — qsu process exited cleanly

```bash
$VMEXEC "$VM" 'pid=$(cat /tmp/54-env.pid 2>/dev/null); if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo still_running; else echo exited; fi'
```

**Assert**: output is `exited`, and S2 already showed the complete
sanitized `/usr/bin/env` output. This check runs from a fresh
vm-exec shell, so `wait` cannot recover the original child exit
status and may report 127 for a non-child pid after the qsu process
has already exited.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/54-*.log /tmp/54-*.err /tmp/54-*.pid'
B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Notes for the runner

- The `--keep-env VAR` flag is documented in `doc/sudo.md` as a
  policy-gated opt-in for preserving specific variables. The
  v1 qsu in this tree (per `qsu.py` docstring) does NOT yet
  implement `--keep-env`; the scenario does NOT test that flag.
  If `--keep-env` lands later, expand this scenario to verify
  the policy gate.
- `subprocess.Popen(env=env, ...)` only takes effect at the
  Linux execve level. The privileged child sees exactly the
  keys in the python dict and nothing else. A common-mode bug
  is `env={**os.environ, "PATH": "..."}` which inherits LD_*
  by accident; the scenario's grep-zero checks catch that
  regression.
- If S2 shows `LD_PRELOAD=` in the env, that's the regression
  to file with HIGH urgency: the qdistro-root-exec sandboxing
  promise is violated and any approved qsu call becomes a root
  shell.
