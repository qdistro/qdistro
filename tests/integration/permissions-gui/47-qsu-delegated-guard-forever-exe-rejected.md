# 47 — Delegated `forever_exe` on qsu is rejected; sender sees DENIED

<!-- qci:visual: required -->

**Lane: qdwin** (native Wayland, the shipped launcher). Read the "qdwin lane"
section of `AGENTS.md` first: no xdotool, no `DISPLAY=:0`; graded frames come
from `qdwin_screenshot`.

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
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}

# Session up, work/work2 silo fixtures (qsu runs as work), idle locker held
# off and proven unlocked. A nonzero exit is a Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup --silos'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl restart qdistro-root-exec.socket'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'

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
# The shipped launcher, first-paint mode (a nonzero exit FAILS S1).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_app'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'
qdwin_screenshot "$ART/47-s1-empty.png"
```

**Assert** (`47-s1-empty.png`):
- The admin approvals window is visible and fully drawn (the title wait
  proved its title; a partly drawn window is a FAIL on this lane).
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
# The request is in the broker AND displayed as the one row. A timeout FAILS S2.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_broker_pending_action qsu.exec:root 30'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals \(1 pending\)" 30'
qdwin_screenshot "$ART/47-s2-pending.png"
$VMEXEC "$VM" 'cat /tmp/47-qsu-caller-uid.txt; ps -o uid=,comm= -u work | grep -w qsu || true'
```

**Assert** (`47-s2-pending.png`):
- One pending row, action prefix `qsu.exec:root`.
- The caller precheck printed uid `2000`; if it printed `0`, report
 ERROR because the scenario launched qsu as root and any cache assertion
 would be meaningless.
- Detail pane shows `argv=/bin/true` (or `argv[00]=/bin/true`).
- Scope radios visible include `Forever, only this exact program`
  and `Forever, only this exact argv tuple`.

### S3 — admin picks `forever_exe`, presses Approve

```bash
# Focus the admin window through the compositor, then select the
# forever_exe scope via the admin app's deterministic keyboard shortcut
# Ctrl+Shift+<index> (index 5 = forever_exe; index 6 = forever_argv).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
virsh -c qemu:///session send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_LEFTSHIFT KEY_5
# A radio tick publishes no state a waiter can read: settle, then capture.
sleep 1
qdwin_screenshot "$ART/47-s3a-forever-exe-selected.png"

# virsh-send Ctrl+Y for Approve. The broker refuses forever_exe on the
# delegated path; the app's refusal surface is a modal titled
# `Decision not recorded`, its own toplevel. Record whether the compositor
# mapped it (the expected shape), then capture.
virsh -c qemu:///session send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
s3_modal_rc=0
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "Decision not recorded" 30' || s3_modal_rc=$?
echo "s3 modal-wait rc=$s3_modal_rc"
qdwin_screenshot "$ART/47-s3b-after-approve-click.png"
```

**Assert**:
- `47-s3a-forever-exe-selected.png` shows the `forever_exe`
  radio filled (NOT the default `once`).
- `s3 modal-wait rc=0` and `47-s3b-after-approve-click.png` shows the
  `Decision not recorded` modal mentioning `ScopeNotPermitted` or
  `not permitted for delegated` (or the friendly `Scope not permitted`
  copy). A nonzero modal-wait rc is acceptable only if the frame shows an
  inline error with that reason and the pending row still visible (the app
  caught the exception; the decide did not persist); with neither, S3 FAILS.
- The qsu sender did NOT unblock as allowed. Check:
  ```bash
  $VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 47-qsu; if bg_rc 47-qsu >/dev/null 2>&1; then echo EXITED; else echo STILL_RUNNING; fi'
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
# window. The compositor's window list is the oracle: qdwin_window_handle
# exits 1 when no live window carries the title, and a FAILED probe (no
# compositor) is reported as probe-error, never as absent/dismissed.
DLG_B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
qdwin_windows >/dev/null || { echo "DIALOG=probe-error"; exit 2; }
if ! qdwin_window_handle "Decision not recorded" >/dev/null; then
  echo "DIALOG=absent"; exit 0
fi
qdwin_focus_window "Decision not recorded" >/dev/null || { echo "DIALOG=probe-error (focus)"; exit 2; }
echo "DIALOG=open"
EOF
)
GONE_B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
for _ in $(seq 1 20); do
  qdwin_windows >/dev/null || { echo "DIALOG=probe-error"; exit 2; }
  qdwin_window_handle "Decision not recorded" >/dev/null || { echo "DIALOG=gone"; exit 0; }
  sleep 0.5
done
echo "DIALOG=still-open"; exit 1
EOF
)
dlg_state=
for attempt in 1 2; do
  if ! out=$($VMEXEC "$VM" "echo $DLG_B64 | base64 -d | bash"); then
    echo "[s4-dismiss $attempt] find: $out"; dlg_state=probe-error; break
  fi
  echo "[s4-dismiss $attempt] find: $out"
  case "$out" in
    *DIALOG=absent*) dlg_state=absent; break ;;
    *DIALOG=open*) ;;
    *) dlg_state=probe-error; break ;;
  esac
  virsh -c qemu:///session send-key "$VM" --codeset linux KEY_ENTER || { dlg_state=probe-error; break; }
  if out=$($VMEXEC "$VM" "echo $GONE_B64 | base64 -d | bash"); then
    echo "[s4-dismiss $attempt] gone: $out"; dlg_state=dismissed; break
  else
    rc=$?
    echo "[s4-dismiss $attempt] gone: $out (rc=$rc)"
    [ "$rc" -eq 1 ] || { dlg_state=probe-error; break; }
    dlg_state=still-open
  fi
done
echo "S4-DIALOG-STATE=$dlg_state"

# Focus the admin window, then select the forever_argv scope via the
# deterministic keyboard shortcut Ctrl+Shift+6 (index 6 = forever_argv).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
virsh -c qemu:///session send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_LEFTSHIFT KEY_6
sleep 1
qdwin_screenshot "$ART/47-s4a-forever-argv-selected.png"

virsh -c qemu:///session send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y

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
  the S3b and S4a frames. `probe-error` (the compositor's window list,
  vm-exec or virsh could not observe or drive the windows) is an ERROR, not a PASS: report it
  with the printed `[s4-dismiss ...]` lines and vm-exec stderr.
- `47-s4a-forever-argv-selected.png` shows no `Decision not
  recorded` modal, and shows `forever_argv` radio
  filled.
- `bg_log 47-qsu` shows no error; the qsu process has
  exited with rc=0 (`/bin/true` succeeded after admin approval).
- Cache table:
  ```bash
  SQL_B64=$(base64 -w0 <<'EOF'
  source /tmp/qci-gui-waiters.sh || exit 2
  qci_sqlite approvals "SELECT caller_uid, action, match_kind, match_value, argv, scope FROM approvals WHERE action LIKE 'qsu.exec:%';"
  EOF
  )
  $VMEXEC "$VM" "echo $SQL_B64 | base64 -d | bash"
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
SQL_B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh || exit 2
qci_sqlite audit "SELECT decision, scope, substr(source, 1, 20) FROM audit WHERE action='qsu.exec:root' ORDER BY id DESC LIMIT 3;"
EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | bash"
```

**Assert**: the newest audit row records `decision=1`,
`scope=forever_argv`, NOT `scope=forever_exe`. If a row with
`scope=forever_exe` appears at all, it must have `decision=0`
(rejected) — the broker must not silently downgrade an admin's
forever_exe pick to a successful allow under any other scope.

## Teardown

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
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
