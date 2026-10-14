# 62 — approving as admin asks for the admin password (cancel leaves it pending)

<!-- qci:visual: required -->
<!-- qci:polkit-test-grant: none -->

**Lane: qdwin** (native Wayland, the shipped launcher). Read the "qdwin lane"
section of `AGENTS.md` first: no xdotool, no `DISPLAY=:0`; graded frames come
from `qdwin_screenshot`.

**What**: an admin-app approval needs the polkit action
`org.qdistro.broker.approve`, which only the admin password creates. With one
pending request, Ctrl+Y in the admin app makes the broker answer
`AuthRequired`; the app asks polkitd for the action for its own process,
polkitd hands it to `qdistro-polkit-agent`, and the agent (forced PAM route)
shows its "Authentication Required" password dialog. Cancel → the app says
"Not authorized" and the request stays pending. Ctrl+Y again → the dialog
again → typing the admin password approves the request. A second request is
then approved with NO dialog (`auth_admin_keep` keeps the grant for the
app's process).

**Why**: before this, any admin-session process could approve its own
`qsu` root request; the broker's /proc identity checks cannot stop a process
that rewrites its own argv to look like the app (sol bsa r4). The password
is the boundary now. Every other approval scenario runs with the qci gate's
test-only YES grant for the action (`grant_broker_approve_for_tests` in
`ci/lib/gates/gui.sh`); this one opts out (`qci:polkit-test-grant: none`
above) because it is the one that tests the real prompt.

**Test-VM credential**: the admin password on qci VMs is `Pa_ssw0rd45`
(`scripts/vm/fresh-vm-bootstrap.sh`, `scripts/vm/build-baseweed-from-scratch.sh`;
also in `AGENTS.md` "Environment").

**Two CI-VM adaptations, and what they do NOT change** (read before grading):

1. *A logind login session for the agent.* On CI VMs the admin session runs
   under a lingering user manager, not a logind seat session, so the agent
   has no session to register for (`presentation-gui/03` scope note). Setup
   opens a real PAM `login` session for admin on tty6 — exactly as
   `tests/integration/vm/polkit-agent-session.bats` does — and waits for
   the agent to register for it. polkitd resolves the admin app (started in
   the admin's user slice) to that session via the user's display session.
2. *An "authenticate" rule instead of "active session".* That tty6 session is
   not the foreground VT, so polkit's `allow_active` default does not apply
   to it (`allow_inactive` is `no`). Setup installs a temporary rule that
   returns `AUTH_ADMIN_KEEP` for admin on this action — the same requirement
   `allow_active` states. It grants nothing: the password is still required,
   and S2 proves it (a cancelled prompt leaves the request pending).

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot, qdwin_click, qdwin_type_lower, qdwin_chord (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}

# Session up, work/work2 silo fixtures, idle locker held off and proven
# unlocked. A nonzero exit is a Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup --silos'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 2>/dev/null; true"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'

B64=$(base64 -w0 <<'EOF'
set -e
source /tmp/qci-gui-waiters.sh
# The gate's test-only YES grant must be absent (this file opts out; remove
# it anyway in case the VM ran another scenario first).
rm -f /etc/polkit-1/rules.d/10-qci-broker-approve-test.rules
test ! -e /etc/polkit-1/rules.d/10-qci-broker-approve-test.rules
# The shipped action, with its shipped defaults.
pkaction --action-id org.qdistro.broker.approve --verbose
# Adaptation 2: authenticate (never YES) for admin, whatever the session's
# activity. Removed in Teardown.
cat >/etc/polkit-1/rules.d/11-qci-62-broker-approve-auth.rules <<'RULE'
// permissions-gui/62 only: require admin authentication for the broker
// approve action even though the CI login session is not the active VT.
polkit.addRule(function(action, subject) {
    if (action.id == "org.qdistro.broker.approve" && subject.user == "admin") {
        return polkit.Result.AUTH_ADMIN_KEEP;
    }
});
RULE
chmod 0644 /etc/polkit-1/rules.d/11-qci-62-broker-approve-auth.rules
# Adaptation 1: a real logind login session for admin, for the agent.
cur=$(journalctl -n0 --show-cursor 2>/dev/null | sed -n 's/^-- cursor: //p')
systemctl stop qci-62-login.service 2>/dev/null || true
systemd-run --unit=qci-62-login -p PAMName=login -p User=admin \
    -p TTYPath=/dev/tty6 -p StandardInput=tty -p StandardOutput=journal \
    /usr/bin/sleep 1800
await_journal_line_after_cursor "$cur" 'registered as session polkit agent' 45 1 -u qdistro-polkit-agent.service
echo SETUP-OK
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert (setup):** the output ends with `SETUP-OK`, and the `pkaction`
output shows `implicit any: no`, `implicit inactive: no`,
`implicit active: auth_admin_keep`. Anything else is a Setup ERROR (the
action is not installed as shipped, or the agent never registered).

The admin app is started INSIDE the admin's user manager (as qdshell's
launcher starts it), not from the vm-exec shell: polkitd resolves a process
to a session through its user slice, and a process left in the guest
agent's system cgroup has no session and gets no agent.

```bash
# Starts the shipped launcher (first-paint mode) in a user scope, then waits
# for qdwin to map the app's window. Prints the app pid; nonzero = Setup ERROR.
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
out=$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
    systemd-run --user --scope --quiet --unit=qci-62-admin-app \
    env QDISTRO_ADMIN_APP_WAIT_PAINTED=1 /usr/local/bin/qdistro-start-admin-app) || exit 1
pid=$(printf '%s\n' "$out" | sed -n '/^[0-9][0-9]*$/p' | tail -n1)
[ -n "$pid" ] || exit 1
QCI_AWAIT_QUIET=1 await_qdwin_window_mapped_pid "$pid" 30 0.2 >/dev/null || exit 1
grep -q '^0::/user.slice/user-1000.slice/' "/proc/$pid/cgroup" || { cat "/proc/$pid/cgroup"; exit 1; }
echo "APP-PID=$pid"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_qdwin_window_title "admin approvals" 45'
```

## Steps

**Readiness is mechanical — run every `await_*` line as written.** A waiter
that TIMES OUT is that step's failure; keep the frame as evidence. The
password dialog is a separate window titled `Authentication Required`; on
qdwin a newly mapped prompt may not receive keyboard focus
(`presentation-gui/03`), so the steps CLICK into it rather than typing
blind.

### S1 — one pending request; Ctrl+Y opens the password dialog

```bash
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start work1 work 'python3 /usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_broker_pending_action test.action 30'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_qdwin_window_title "admin approvals \(1 pending\)" 30'

# Journal cursors are kept on the HOST and passed in: no fixed guest paths.
cursor() { $VMEXEC "$VM" 'journalctl -n0 --show-cursor 2>/dev/null | sed -n "s/^-- cursor: //p"' | tail -n1; }
C1=$(cursor)
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
virsh -c qemu:///session send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
# The broker refused (AuthRequired) and the app asked polkit interactively:
# the agent logs the forced PAM route for THIS action.
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh; await_journal_line_after_cursor '$C1' 'polkit BeginAuth: action=org.qdistro.broker.approve method=pam\$' 30 1 -u qdistro-polkit-agent.service"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_qdwin_window_title "Authentication Required" 30'
qdwin_screenshot "$ART/62-s1-password-dialog.png"
# The request is still pending while the dialog is up: nothing was decided.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; broker_pending_count'
```

**Assert (S1):**
- The agent journal line `polkit BeginAuth: action=org.qdistro.broker.approve method=pam`
  appeared (method `pam`, never `broker`).
- OPEN the frame: an "Authentication Required" dialog is visible with the
  message "Authentication is required to approve qdistro permission requests
  or change approval rules", an `Action: org.qdistro.broker.approve` line, a
  `Password:` field and OK/Cancel buttons. The admin window behind it still
  lists the pending request.
- `broker_pending_count` prints `1`.

### S2 — Cancel: "Not authorized", the request stays pending

Click the dialog's **Cancel** button using coordinates read from the S1
frame you opened: `qdwin_mouse_move X Y`, `sleep 0.5`, `qdwin_click X Y`.
Then:

```bash
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh; await_journal_line_after_cursor '$C1' 'action=org.qdistro.broker.approve method=pam -> deny \(prompt-cancelled\)' 30 1 -u qdistro-polkit-agent.service"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_qdwin_window_title "Decision not recorded" 30'
qdwin_screenshot "$ART/62-s2-not-authorized.png"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; broker_pending_count'
# The requester is still waiting (not denied).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; echo "work1-rc=$(bg_rc work1)"'
# Dismiss the modal (its OK button has focus in the app's own window).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "Decision not recorded"'
virsh -c qemu:///session send-key "$VM" --codeset linux KEY_ENTER
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_qdwin_window_title "admin approvals \(1 pending\)" 30'
```

**Assert (S2):**
- The agent logged `-> deny (prompt-cancelled)` for the action.
- OPEN the frame: the app's "Decision not recorded" modal reads "Not
  authorized: Authentication was cancelled or not accepted, so nothing was
  changed. The request is still pending." It must NOT say the request was
  denied.
- `broker_pending_count` prints `1`, and `work1-rc=` is empty (still running:
  the requester got no answer, in particular no deny).

### S3 — Ctrl+Y again, type the admin password: approved

```bash
C3=$(cursor)
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
virsh -c qemu:///session send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_qdwin_window_title "Authentication Required" 30'
qdwin_screenshot "$ART/62-s3-password-dialog-again.png"
```

Click into the **Password:** field using coordinates read from that frame
(`qdwin_mouse_move X Y`, `sleep 0.5`, `qdwin_click X Y`), then type the
admin password `Pa_ssw0rd45` and press Enter:

```bash
qdwin_chord shift -- p
qdwin_type_lower a
qdwin_chord shift -- minus
qdwin_type_lower ssw0rd45
qdwin_send_key KEY_ENTER
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh; await_journal_line_after_cursor '$C3' 'action=org.qdistro.broker.approve method=pam -> allow \(pam-ok\)' 60 1 -u qdistro-polkit-agent.service"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_qdwin_window_title "admin approvals" 30'
qdwin_screenshot "$ART/62-s3-approved.png"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait work1 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log work1; echo "rc=$(bg_rc work1)"'
# The audit row: approved by the admin uid through the app (approver 1000).
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh; qci_sqlite audit \"SELECT caller_uid, decision, approver_uid FROM audit WHERE action='test.action' ORDER BY id DESC LIMIT 1;\""
```

If the agent logged `-> deny` with a PAM reason instead (wrong characters
typed), that is a harness typing failure, not a product verdict: record
ERROR with the agent line, do not retry more than once.

**Assert (S3):**
- The agent logged `-> allow (pam-ok)` for the action.
- OPEN the S3 dialog frame: the same dialog as S1 appeared again (the
  cancelled attempt left no grant behind).
- OPEN the approved frame: the Pending list is empty, no dialog is visible.
- The SDK log contains `ALLOWED` on its own line.
- The audit row printed last is `2000|1|1000`.

### S4 — the grant is kept: a second approval does not prompt

```bash
C4=$(cursor)
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start work2 work 'python3 /usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_qdwin_window_title "admin approvals \(1 pending\)" 30'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
virsh -c qemu:///session send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait work2 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log work2; echo "rc=$(bg_rc work2)"'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_qdwin_window_title "admin approvals" 30'
qdwin_screenshot "$ART/62-s4-kept-no-prompt.png"
$VMEXEC "$VM" "n=\$(journalctl -u qdistro-polkit-agent.service --after-cursor '$C4' --no-pager -o cat | grep -c 'BeginAuth: action=org.qdistro.broker.approve method=pam\$'); echo s4-beginauth=\$n"
```

**Assert (S4):**
- The SDK log contains `ALLOWED`.
- `s4-beginauth=0`: no new authentication was started for the action.
- OPEN the frame: empty Pending list and no "Authentication Required"
  dialog.

## Teardown

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'systemctl stop qci-62-login.service 2>/dev/null; rm -f /etc/polkit-1/rules.d/11-qci-62-broker-approve-auth.rules ; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
```

## Screenshots
- `62-s1-password-dialog.png` — the agent's password dialog over the admin app
- `62-s2-not-authorized.png` — the app's "Not authorized" modal after Cancel
- `62-s3-password-dialog-again.png` — the dialog again on the second Ctrl+Y
- `62-s3-approved.png` — empty Pending list after the password was accepted
- `62-s4-kept-no-prompt.png` — second approval, no dialog
