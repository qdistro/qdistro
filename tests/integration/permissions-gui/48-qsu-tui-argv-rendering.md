# 48 — TUI renders qsu argv on its own `Argv:` line, not 30 noisy details

**What**: launch `qdistro-admin-tui`. As `work`, invoke
`/usr/local/bin/qsu /bin/sh -c "echo hi"` — an argv with 3
elements (`/bin/sh`, `-c`, `echo hi`). The TUI's right detail
pane must render:
- `Argv: /bin/sh -c 'echo hi'` (the shlex-joined argv on a single
  line, bold).
- `Details: target_user=root` (the OTHER details — argv[00..02]
  must NOT appear in the Details list, because
  `_split_argv_from_details` strips them out).

Then press `6` to select scope `forever_argv` and `a` to approve.
qsu unblocks; verify cache row landed.

**Why**: `tui/qdistro_admin_tui.py:144-149` (the `if argv_line is
not None: text += f"Argv: [b]{argv_line}[/b]\\n"` branch) is a
load-bearing UX improvement: without it, a qsu prompt with a
30-element argv would render 30 `argv[NN]=…` lines inside Details
and push the actual semantic content off-screen. The function
`_split_argv_from_details` extracts the `argv[NN]` keys and
returns a shlex-joined display. No GUI test currently asserts
this rendering — a regression where the argv keys leak back into
the `details` rendering would degrade the qsu admin path without
breaking any backend test.

## Setup

```bash
VM=${VMNAME:-qd-sudo}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
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
```

## Steps

### S1 — launch TUI

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-tui'
sleep 4
$VMGUI "$VM" screenshot /tmp/48-s1-tui-launched.png
```

**Assert** (`/tmp/48-s1-tui-launched.png`): qterminal window
visible with TUI content showing `qdistro admin approvals (TUI)`
title and an empty queue table.

### S2 — qsu invocation with multi-element argv

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /bin/sh -c "echo hi" \
  >/tmp/48-qsu.log 2>&1 & echo $! >/tmp/48-qsu.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/48-s2-tui-pending.png
```

**Assert** (`/tmp/48-s2-tui-pending.png`):
- TUI's left queue table has one row for uid 2000, action
  `qsu.exec:root`.
- TUI's right detail pane contains, on separate lines:
  - `uid=2000  pid=…` (chip-coloured).
  - `Action: qsu.exec:root`.
  - exe path ending in `/usr/local/bin/qsu` in dim text.
  - **`Argv: /bin/sh -c 'echo hi'`** — bold, on its OWN line.
    The argument `echo hi` must appear quoted (`'echo hi'`)
    because shlex.join handles the embedded space.
  - **`Details: target_user=root`** — the `argv[NN]` keys are
    NOT in this line.

  If the runner sees `Details:` containing `argv[00]=/bin/sh,
  argv[01]=-c, argv[02]=echo hi`, that's the regression to flag —
  `_split_argv_from_details` is not consuming the keys correctly.

### S3 — press `6` for forever_argv

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "qterminal" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# Per AGENTS.md pitfall 3b: plain keys on qterminal are flaky via
# xdotool/vm-gui; use virsh send-key to inject at evdev layer.
virsh send-key "$VM" --codeset linux KEY_6
sleep 1
$VMGUI "$VM" screenshot /tmp/48-s3-scope-forever-argv.png
```

**Assert** (`/tmp/48-s3-scope-forever-argv.png`):
- Detail pane's Scope line reads `Scope: Forever, only this
  exact argv tuple` (bold) — the human label resolved from
  `SCOPES['forever_argv']` in
  `tui/qdistro_admin_tui.py`.

### S4 — press `a` to approve; qsu unblocks

```bash
virsh send-key "$VM" --codeset linux KEY_A
sleep 2

$VMEXEC "$VM" 'wait $(cat /tmp/48-qsu.pid) 2>/dev/null; cat /tmp/48-qsu.log'
$VMGUI "$VM" screenshot /tmp/48-s4-after-approve.png
```

**Assert**:
- `/tmp/48-qsu.log` contains `hi` (sh ran the echo).
- `/tmp/48-s4-after-approve.png`: TUI queue table empty again.
- Cache row:
  ```bash
  SQL_B64=$(base64 -w0 <<'SQL_EOF'
  SELECT match_kind, scope FROM approvals WHERE action='qsu.exec:root';
  SQL_EOF
  )
  $VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
  ```
  Output: `argv_exact|forever_argv`.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/48-*.log /tmp/48-*.pid'
B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Notes for the runner

- All TUI key inputs (`6`, `a`, `r`) must go via
  `virsh send-key --codeset linux KEY_…`, NOT `vm-gui key`. See
  AGENTS.md pitfall 3b — qterminal under labwc swallows plain
  keystrokes through xdotool inconsistently.
- The qterminal window must have keyboard focus when `6` is
  injected — that's what `windowactivate --sync` is for. If S3
  shows the scope unchanged, the focus didn't transfer; retry
  after `wmctrl -a qterminal` or click the qterminal title bar.
- This scenario covers only the qsu argv-detail case. The
  generic scope-picker keys are covered by scenario 02
  (`tui-scope-picker.md`).
