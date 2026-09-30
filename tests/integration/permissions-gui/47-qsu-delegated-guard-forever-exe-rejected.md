# 47 — Delegated `forever_exe` on qsu is rejected; sender sees DENIED

<!-- qci:visual: required -->

**What**: as `work` (uid 2000) invoke `/usr/local/bin/qsu /bin/true`.
The pending request reaches the admin app via the delegated path
(`RequestPermissionAs`). Admin selects the legacy `Forever, only this
exact program` radio (scope=`forever_exe`) and clicks Approve. The
broker rejects the decision with `ScopeNotPermitted` because
`forever_exe` is in `_DELEGATED_FORBIDDEN_SCOPES`. The admin app's
selected scope is unwound (the pending row reappears or remains)
and the qsu sender — still blocking on `WaitForDecision` — does NOT
unblock with allow; on the runner's deny follow-up the sender sees
`request denied` and rc=1.

**Why**: `doc/sudo.md` §"Argv pinning and the delegated path"
documents why qsu loses the broad `forever_exe` scope under
delegation — an unauthenticated future caller at the same uid would
inherit a blanket "run any argv at this exe" grant. The broker's
`_DELEGATED_FORBIDDEN_SCOPES = frozenset(("1h", "24h", "forever",
"forever_exe"))` is the load-bearing guard. Unit tests pin
`DecideRequest` returning the exception; this scenario pins the
end-to-end UX — admin sees the rejection rather than the broker
silently downgrading to `once`.

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

# qsu must be a normal user-owned client process from the broker's point of
# view; qdistro-root-exec captures the connecting peer's SO_PEERCRED uid and
# forwards that to RequestPermissionAs. A setuid or root-launched qsu would
# incorrectly mint rows for uid 0 and invalidate the scenario.
$VMEXEC "$VM" 'stat -c "qsu-mode=%a owner=%U group=%G" /usr/local/bin/qsu; test "$(stat -c %a /usr/local/bin/qsu)" = 755'

B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Steps

### S1 — launch admin app

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
$VMGUI "$VM" screenshot /tmp/47-s1-empty.png
```

**Assert** (`/tmp/47-s1-empty.png`):
- Window titled `admin approvals` visible.
- Pending list empty.

### S2 — invoke qsu as work; pending row carries argv

```bash
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
runuser -u work -- bash -c 'id -u > /tmp/47-qsu-caller-uid.txt'
bg_start 47-qsu work '/usr/local/bin/qsu /bin/true'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/47-s2-pending.png
$VMEXEC "$VM" 'cat /tmp/47-qsu-caller-uid.txt; ps -o uid=,pid=,comm= -p "$(cat /tmp/47-qsu.pid)" 2>/dev/null || true'
```

**Assert** (`/tmp/47-s2-pending.png`):
- One pending row, action prefix `qsu.exec:root`.
- The caller precheck printed uid `2000`; if it printed `0`, report
 ERROR because the scenario launched qsu as root and any cache assertion
 would be meaningless.
- Detail pane shows `argv=/bin/true` (or `argv[00]=/bin/true`).
- Scope radios visible include `Forever, only this exact program`
  and `Forever, only this exact argv tuple`.

### S3 — admin picks `forever_exe`, presses Approve

```bash
# Activate the admin window, then select the forever_exe scope via the
# admin app's deterministic keyboard shortcut Ctrl+Shift+<index> (index
# 5 = forever_exe; index 6 = forever_argv). Mouse-clicking the radio is
# unreliable on the Qt/XWayland template, so use the shortcut instead.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_LEFTSHIFT KEY_5
sleep 1
$VMGUI "$VM" screenshot /tmp/47-s3a-forever-exe-selected.png

# virsh-send Ctrl+Y for Approve.
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 2
$VMGUI "$VM" screenshot /tmp/47-s3b-after-approve-click.png
```

**Assert**:
- `/tmp/47-s3a-forever-exe-selected.png` shows the `forever_exe`
  radio filled (NOT the default `once`).
- `/tmp/47-s3b-after-approve-click.png` shows either:
  - a QMessageBox / inline error mentioning `ScopeNotPermitted` or
    `not permitted for delegated`, OR
  - the pending row still visible (admin app caught the exception;
    decide didn't persist).
- The qsu sender did NOT unblock as allowed. Check:
  ```bash
  $VMEXEC "$VM" 'cat /tmp/47-qsu.log; kill -0 $(cat /tmp/47-qsu.pid) 2>/dev/null && echo STILL_RUNNING || echo EXITED'
  ```
  Expected: `STILL_RUNNING` (the qsu client is still waiting because
  the decide failed), or the log contains `request denied` if the
  broker recorded the rejection as a deny. Either is acceptable —
  the assertion is "the sender did NOT get a forever_exe allow."

### S4 — admin re-picks `forever_argv` and approves cleanly

The argv-pinned scope IS permitted on the delegated path. This step
proves the broker isn't broken — only the over-broad scope is
rejected.

```bash
# S3's rejection is a modal QMessageBox titled "Decision not recorded" (the
# admin app's designed refusal surface, S3's PASS shape). While it is open it
# owns the keyboard: Ctrl+Shift+6 and Ctrl+Y below would go to the modal, not
# the main window, and the S4 frame would show the modal. Dismiss it with its
# default OK button (Enter) and prove it is gone before S4 touches the main
# window.
#
# Every probe distinguishes "no such window" from "could not look":
# `xdotool search` exits 1 on no match, but also on a dead display, so a
# dialog miss counts only when the SAME probe still finds the main
# `admin approvals` window. Any other xdotool status, a failed activation or
# a failed vm-exec/virsh call is `probe-error`, never `absent`/`dismissed`.
# Guest exit codes: 0 = condition met, 1 = dialog still open, 2 = probe error.
DLG_LIB=$(cat <<'EOF'
# xsearch <name-regex>: print visible window ids; 0 found, 1 none, 2 error.
xsearch() {
  local out rc
  out=$(runuser -u admin -- env DISPLAY=:0 timeout 10 xdotool search \
    --onlyvisible --name "$1"); rc=$?
  case "$rc" in
    0) [ -n "$out" ] || { echo "xsearch '$1': rc=0 but no id" >&2; return 2; }
       printf '%s\n' "$out"; return 0 ;;
    1) [ -z "$out" ] || { echo "xsearch '$1': rc=1 with output" >&2; return 2; }
       return 1 ;;
    *) echo "xsearch '$1': xdotool rc=$rc" >&2; return 2 ;;
  esac
}
# dialog_state: 0 dialog visible (id on stdout), 1 dialog not visible while
# the main window is, 2 probe error.
dialog_state() {
  local ids rc
  ids=$(xsearch '^Decision not recorded$'); rc=$?
  [ "$rc" -eq 0 ] && { printf '%s\n' "$ids" | head -n1; return 0; }
  [ "$rc" -eq 1 ] || return 2
  xsearch '^admin approvals' >/dev/null; rc=$?
  [ "$rc" -eq 0 ] && return 1
  echo "dialog_state: main window not visible either (xsearch rc=$rc)" >&2
  return 2
}
EOF
)
DLG_FIND=$(base64 -w0 <<EOF
$DLG_LIB
for _ in \$(seq 1 20); do
  wid=\$(dialog_state); rc=\$?
  [ "\$rc" -eq 2 ] && { echo "DIALOG=probe-error"; exit 2; }
  [ "\$rc" -eq 0 ] && break
  sleep 0.5
done
[ "\$rc" -eq 1 ] && { echo "DIALOG=absent"; exit 0; }
runuser -u admin -- env DISPLAY=:0 timeout 10 xdotool windowactivate --sync "\$wid" \
  || { echo "DIALOG=probe-error (windowactivate \$wid failed)"; exit 2; }
echo "DIALOG=open wid=\$wid"
EOF
)
DLG_GONE=$(base64 -w0 <<EOF
$DLG_LIB
for _ in \$(seq 1 20); do
  dialog_state >/dev/null; rc=\$?
  [ "\$rc" -eq 1 ] && { echo "DIALOG=gone"; exit 0; }
  [ "\$rc" -eq 2 ] && { echo "DIALOG=probe-error"; exit 2; }
  sleep 0.5
done
echo "DIALOG=still-open"; exit 1
EOF
)
dlg_state=
for attempt in 1 2; do
  if ! out=$($VMEXEC "$VM" "echo $DLG_FIND | base64 -d | bash"); then
    echo "[s4-dismiss $attempt] find: $out"; dlg_state=probe-error; break
  fi
  echo "[s4-dismiss $attempt] find: $out"
  case "$out" in
    *DIALOG=absent*) dlg_state=absent; break ;;
    *DIALOG=open*) ;;
    *) dlg_state=probe-error; break ;;
  esac
  virsh send-key "$VM" --codeset linux KEY_ENTER || { dlg_state=probe-error; break; }
  if out=$($VMEXEC "$VM" "echo $DLG_GONE | base64 -d | bash"); then
    echo "[s4-dismiss $attempt] gone: $out"; dlg_state=dismissed; break
  else
    rc=$?
    echo "[s4-dismiss $attempt] gone: $out (rc=$rc)"
    [ "$rc" -eq 1 ] || { dlg_state=probe-error; break; }
    dlg_state=still-open
  fi
done
echo "S4-DIALOG-STATE=$dlg_state"

# Activate the admin window, then select the forever_argv scope via the
# deterministic keyboard shortcut Ctrl+Shift+6 (index 6 = forever_argv).
# This replaces the mouse-click on the radio, which is unreliable on the
# Qt/XWayland template and was leaving the default `once` selected — the
# broker then correctly recorded `once` and S4 failed even though the
# broker is not buggy.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_LEFTSHIFT KEY_6
sleep 1
$VMGUI "$VM" screenshot /tmp/47-s4a-forever-argv-selected.png

virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 2

# bg_wait, never `wait $(cat X.pid)` — that does not wait in a separate guest
# shell (AGENTS.md, "A backgrounded job"). A TIMEOUT here IS this step's failure.
# `echo "rc=$?"` after a `cat` reported the CAT's status, never qsu's.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 47-qsu 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 47-qsu; echo "rc=$(bg_rc 47-qsu)"'
```

**Assert**:
- The dismissal printed `S4-DIALOG-STATE=dismissed`. `absent` is
  acceptable only when the S3b frame shows no modal (S3 passed on its
  inline-only shape); `absent` after an S3b frame WITH the modal is an S4
  FAIL. `still-open` (the modal did not close on OK) is an S4 FAIL with
  the S3b and S4a frames. `probe-error` (xdotool, vm-exec or virsh could
  not observe or drive the windows) is an ERROR, not a PASS: report it
  with the printed `[s4-dismiss ...]` lines and vm-exec stderr.
- `/tmp/47-s4a-forever-argv-selected.png` shows no `Decision not
  recorded` modal, and shows `forever_argv` radio
  filled.
- `/tmp/47-qsu.log` is empty or has no error; the qsu process has
  exited with rc=0 (`/bin/true` succeeded after admin approval).
- Cache table:
  ```bash
  SQL_B64=$(base64 -w0 <<'SQL_EOF'
  SELECT caller_uid, action, match_kind, match_value, argv, scope
    FROM approvals WHERE action LIKE 'qsu.exec:%';
  SQL_EOF
  )
  $VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
  ```
  Output: one row
  `2000|qsu.exec:root|argv_exact|...|["/bin/true"]|forever_argv`.
  If the row has `caller_uid=0`, report FAIL and include the qsu mode,
  caller precheck, and `qdistro-root-exec` journal lines; the delegated
  path lost the original caller uid.
  The match_kind is `argv_exact` (forever_argv → argv_exact in
  `_VALID_SCOPES`); `match_value` carries the executable path and
  `argv` carries the pinned argv JSON.

### S5 — broker-side audit shows the rejected scope was not committed

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT decision, scope, substr(source, 1, 20) FROM audit
  WHERE action='qsu.exec:root'
  ORDER BY id DESC LIMIT 3;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: the newest audit row records `decision=1`,
`scope=forever_argv`, NOT `scope=forever_exe`. If a row with
`scope=forever_exe` appears at all, it must have `decision=0`
(rejected) — the broker must not silently downgrade an admin's
forever_exe pick to a successful allow under any other scope.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/47-*.log /tmp/47-*.pid'
B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Notes for the runner

- The Qt admin app currently surfaces broker exceptions via
  `QMessageBox.critical` on `DecideRequest` failure. If you see a
  plain modal with `ScopeNotPermitted` in the body, that is the
  PASS shape for S3. If the admin app silently swallows the
  exception (no dialog, no log, the row vanishes anyway), report
  FAIL with a screenshot — silent broker-error swallowing is a UX
  regression worth fixing.
- The qsu socket-activation unit (`qdistro-root-exec.socket`) must
  be running for `/usr/local/bin/qsu` to reach the privileged exec
  service. The Setup restart covers this; if S2 yields no pending
  row within 2s, check `/run/qdistro-root-exec/sock` exists.
- `forever_exe` is the LEGACY scope name — `_VALID_SCOPES` maps it
  to `match_kind='exe_only'` (any argv at the same caller_exe). The
  scenario name keeps the UI label `Forever, only this exact
  program` aligned with the radio text in `admin_app/qdistro_admin_app.py`.
