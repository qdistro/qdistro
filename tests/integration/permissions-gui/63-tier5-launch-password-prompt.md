# 63 — launching a tier-5 app from the shell asks for the admin password

<!-- qci:visual: required -->
<!-- qci:polkit-test-grant: none -->

**Lane: qdwin** (native Wayland, the shipped shell launcher). Read the "qdwin
lane" section of `AGENTS.md` first: no xdotool, no `DISPLAY=:0`; graded frames
come from `qdwin_screenshot`.

**What**: launching the foot VM row (catalogue appId `tier5/foot`) from
qdshell's launcher runs `qdistro-tier5-spawn --vm <derived name> --policy-key
tier5/foot -- foot` as admin. Its one root step is `pkexec
/usr/libexec/qdistro/qdistro-tier5-bridge`, polkit action
`org.qdistro.tier5.bridge` (`auth_admin_keep`, `allow_inactive=no`). polkitd
hands the authentication to `qdistro-polkit-agent`, which forces the PAM route
for this action and shows its "Authentication Required" password dialog.

- **S1 Cancel** → no bridge, no guest, no window, no launch record.
- **S2 Password** → the agent verifies `Pa_ssw0rd45` through PAM; the bridge
  starts the waypipe client, the broker registers it, the guest boots and the
  foot window appears, tagged `qdistro.tier5.<name>` by the compositor.
- **S3 Second launch** → the dialog appears AGAIN (every launch
  authenticates); the password admits it and a second foot session joins
  the same guest, tagged and registered.
- **S4 Close** → both sessions closed: guest, overlay and every bridge-chain
  process are gone.

**Why**: tier-5 decision record (todo/review/tier5-decisions) steps 8 and 10 —
GUI launch/close/authentication must be accepted live with the real prompt.
Every other tier-5 scenario (19, 20, 21) and the headless drivers install a
temporary YES rule (`/etc/polkit-1/rules.d/10-qdistro-tier5-bridge-test.rules`)
because they test the window path; this one removes it and opts out of the qci
gate's broker-approve test grant (`qci:polkit-test-grant: none` above) because
it is the one that tests authentication.

**Test-VM credential**: the admin password on qci VMs is `Pa_ssw0rd45`
(`scripts/vm/fresh-vm-bootstrap.sh`, `scripts/vm/build-baseweed-from-scratch.sh`).

**Two CI-VM adaptations — the same two `62` makes, and what they do NOT change**:

1. *A logind login session for the agent.* On CI VMs the admin session runs
   under a lingering user manager, not a logind seat session, so the agent
   has no session to register for. Setup opens a real PAM `login` session for
   admin on tty6 (as `tests/integration/vm/polkit-agent-session.bats` does)
   and waits for the agent to register for it. polkitd resolves qdshell's
   spawn (in the admin's user slice) to that session as the user's display
   session.
2. *An "authenticate" rule instead of "active session".* That tty6 session is
   not the foreground VT, so the shipped `allow_inactive=no` would refuse the
   launch outright (`tests/integration/vm/tier5-bridge-enforcing.bats`, case
   `inactive`, proves it). Setup installs a temporary rule returning
   `AUTH_ADMIN_KEEP` for admin on this action — the requirement
   `allow_active` states. It grants nothing: the password is still required,
   and S1 proves it.

**Contract graded by S3: every launch authenticates.** polkit keeps an
`auth_admin_keep` grant only for the authenticated SUBJECT, and pkexec's
subject is its parent process — the `spawn-tier5.sh` wrapper of that launch.
polkitd treats a different process as the same subject only when both share
parent, cgroup and a controlling terminal (none here), and the product keeps
no long-lived helper to carry a grant across launches. So each launch from
the shell is a fresh wrapper, a fresh subject and a fresh password dialog:
S3 PASSES when the dialog appears again and the password admits the second
launch. The keep grant serves only the same caller process re-running pkexec
(`tests/integration/vm/tier5-bridge-enforcing.bats`, `keep-same-caller`).
A second launch that comes up WITHOUT a dialog is a FAIL of S3.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot, qdwin_click, qdwin_mouse_move, qdwin_type_lower, qdwin_chord, qdwin_send_key (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}

# Session up, idle locker held off and proven unlocked. Nonzero = Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup'

B64=$(base64 -w0 <<'EOF'
set -e
source /tmp/qci-gui-waiters.sh
S=/run/qci-63; rm -rf "$S"; install -d -m 0755 "$S"
test -e /dev/kvm
test -f /var/lib/libvirt/images/qdistro-tier5-base.qcow2
test -x /usr/local/bin/qdistro-tier5-spawn
test -x /usr/libexec/qdistro/qdistro-tier5-bridge
# The guest qdshell derives for tier5/foot (VMApps._vmNameForApp; the same
# derivation spawn-tier5.sh / qdistro-tier5-cleanup.sh check).
python3 - tier5/foot > "$S/vmname" <<'PY'
import re, sys
s = sys.argv[1]
fnv, djb = 0x811c9dc5, 0x00001505
for c in (ord(ch) for ch in s):
    fnv = ((fnv ^ c) * 0x01000193) & 0xFFFFFFFF
    djb = ((djb * 33) ^ c) & 0xFFFFFFFF
stub = re.sub(r"[^a-zA-Z0-9_-]", "-", s)
stub = re.sub(r"-{2,}", "-", stub).strip("-")[:43]
print("t5-" + (stub or "vmapp") + f"-{fnv:08x}{djb:08x}")
PY
N=$(cat "$S/vmname")
# No grant of any kind for the action: no YES test rule, no other rule.
rm -f /etc/polkit-1/rules.d/10-qdistro-tier5-bridge-test.rules
if grep -rl 'org.qdistro.tier5.bridge' /etc/polkit-1/rules.d /usr/share/polkit-1/rules.d 2>/dev/null; then
    echo "a polkit rule names org.qdistro.tier5.bridge"; exit 1
fi
pkaction --action-id org.qdistro.tier5.bridge --verbose
# No leftover guest/session from an earlier attempt.
pkill -u admin -f '[s]pawn-tier5.sh --vm t5-tier5-foot-' 2>/dev/null || true
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 HOME=/home/admin \
    /usr/local/bin/qdistro-tier5-cleanup "$N" || true
# Adaptation 2: authenticate (never YES) for admin. Removed in Teardown.
cat >/etc/polkit-1/rules.d/11-qci-63-tier5-bridge-auth.rules <<'RULE'
// permissions-gui/63 only: require admin authentication for the tier-5
// bridge even though the CI login session is not the active VT.
polkit.addRule(function(action, subject) {
    if (action.id == "org.qdistro.tier5.bridge" && subject.user == "admin") {
        return polkit.Result.AUTH_ADMIN_KEEP;
    }
});
RULE
chmod 0644 /etc/polkit-1/rules.d/11-qci-63-tier5-bridge-auth.rules
# Adaptation 1: a real logind login session for admin, for the agent.
cur=$(journalctl -n0 --show-cursor 2>/dev/null | sed -n 's/^-- cursor: //p')
systemctl stop qci-63-login.service 2>/dev/null || true
systemd-run --unit=qci-63-login -p PAMName=login -p User=admin \
    -p TTYPath=/dev/tty6 -p StandardInput=tty -p StandardOutput=journal \
    /usr/bin/sleep 3600
await_journal_line_after_cursor "$cur" 'registered as session polkit agent' 45 1 -u qdistro-polkit-agent.service
# Baseline of the launch chain (bridge, runuser, secctx-exec, waypipe).
pgrep -f '/usr/libexec/qdistro/[q]distro-tier5-bridge' > "$S/chain-before" || true
pgrep -x waypipe >> "$S/chain-before" || true
sqlite3 /var/lib/qdistro/audit/audit.sqlite 'SELECT coalesce(max(id),0) FROM audit;' > "$S/audit-id"
faillock --user admin --reset 2>/dev/null || true
# probe.sh <what> — the mechanical ground truth the steps print.
cat > "$S/probe.sh" <<'PROBE'
#!/bin/bash
S=/run/qci-63; N=$(cat "$S/vmname"); IMG=/home/admin/.local/share/libvirt/images
DB=/var/lib/qdistro/audit/audit.sqlite; A0=$(cat "$S/audit-id")
vsh() { runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 virsh -c qemu:///session "$@"; }
# chain_roots — every process whose argv names the bridge for $N, classified.
# A pkexec still waiting on authentication carries the bridge path in its
# argv and, being setuid, has euid 0 — but its REAL uid is the caller's
# (1000) and its exe is pkexec. A bridge exists only after pkexec has
# authorized and exec'd it: exe = the python interpreter, real uid 0.
chain_roots() {
    local p exe ruid euid
    for p in $(pgrep -f "[q]distro-tier5-bridge --vm $N"); do
        exe=$(readlink "/proc/$p/exe" 2>/dev/null) || continue
        read -r ruid euid < <(awk '/^Uid:/ {print $2, $3}' "/proc/$p/status" 2>/dev/null)
        case "$exe" in
            */pkexec) echo "pkexec pid=$p ruid=$ruid euid=$euid exe=$exe" ;;
            /usr/bin/python3*) [ "$ruid" = 0 ] && echo "bridge pid=$p ruid=$ruid euid=$euid exe=$exe" \
                                              || echo "other pid=$p ruid=$ruid euid=$euid exe=$exe" ;;
            *) echo "other pid=$p ruid=$ruid euid=$euid exe=$exe" ;;
        esac
    done
}
case "$1" in
wrapper)
    w=$(pgrep -u admin -f "[s]pawn-tier5.sh --vm $N --policy-key tier5/foot" | head -1)
    echo "wrapper=$w parent=$(ps -o comm= -p "$(ps -o ppid= -p "${w:-1}" | tr -d ' ')")" ;;
counts)
    echo "wrappers=$(pgrep -u admin -fc "[s]pawn-tier5.sh --vm $N")"
    roots=$(chain_roots)
    echo "bridges=$(grep -c '^bridge ' <<<"$roots")"
    echo "pkexec-pending=$(grep -c '^pkexec ' <<<"$roots")"
    echo "bridge-other=$(grep -c '^other ' <<<"$roots")"
    echo "defined=$(vsh list --all --name | grep -cx "$N")"
    echo "running=$(vsh list --name | grep -cx "$N")"
    echo "qemu=$(pgrep -u admin -fc "[g]uest=$N,")"
    echo "overlay=$(ls "$IMG/$N".qcow2* 2>/dev/null | wc -l)"
    echo "records=$(sqlite3 "$DB" "SELECT count(*) FROM audit WHERE id > $A0 AND action='qdistro.lineage.register:vm-$N';")"
    echo "new-waypipe=$(for p in $(pgrep -x waypipe); do grep -qx "$p" "$S/chain-before" || echo "$p"; done | wc -l)"
    echo "root-files=$(find /home/admin/.local/share/libvirt /run/user/1000 -xdev -user root -newer "$S/vmname" 2>/dev/null | wc -l)"
    [ -n "$roots" ] && printf '%s\n' "$roots" ;;
rows)
    sqlite3 "$DB" "SELECT source FROM audit WHERE id > $A0 AND action='qdistro.lineage.register:vm-$N' ORDER BY id;" ;;
accepted)   # accepted <cursor-file>
    echo "accepted=$(journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service --after-cursor "$(cat "$S/$2.cur")" --no-pager -o cat | grep -c "qdwin/secctx: client accepted engine=qdistro.tier5 app_id=qdistro.tier5.$N ")" ;;
wait-wrappers)   # wait-wrappers <n> — until exactly <n> wrappers run (60 s)
    for _ in $(seq 1 60); do [ "$(pgrep -u admin -fc "[s]pawn-tier5.sh --vm $N")" = "$2" ] && break; sleep 1; done
    echo "wrappers=$(pgrep -u admin -fc "[s]pawn-tier5.sh --vm $N")" ;;
wait-reaped)
    for _ in $(seq 1 90); do
        [ "$(pgrep -u admin -fc "[s]pawn-tier5.sh --vm $N")" = 0 ] && [ "$(pgrep -u admin -fc "[g]uest=$N,")" = 0 ] && break
        sleep 1
    done ;;
esac
PROBE
chmod 0755 "$S/probe.sh"
echo "VMNAME=$N"
echo SETUP-OK
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert (setup):** the output ends with `SETUP-OK`, prints `VMNAME=t5-tier5-foot-…`,
the `grep -rl` found no rule naming the action, and the `pkaction` output shows
`implicit any: auth_admin`, `implicit inactive: no`,
`implicit active: auth_admin_keep` and
`org.freedesktop.policykit.exec.path -> /usr/libexec/qdistro/qdistro-tier5-bridge`.
Anything else is a Setup ERROR.

Two guest helpers used below (cursors and the VM name are kept IN THE GUEST
under `/run/qci-63`, so they survive however you split the blocks):

```bash
# mark <name> — store a system-journal cursor in the guest.
mark() { $VMEXEC "$VM" "journalctl -n0 --show-cursor 2>/dev/null | sed -n 's/^-- cursor: //p' > /run/qci-63/$1.cur"; }
# launch_foot_vm — open the launcher, filter, start the VM row.
launch_foot_vm() {
    $VMGUI "$VM" key ctrl+space
    sleep 1
    # Filter by the catalogue appId: "foot" also lists the admin's own Foot,
    # Foot Client and Foot Server ahead of the VM row; "tier5/foot" matches
    # only the VM row (VMAppsProvider matches appIds), so it is the single,
    # highlighted result.
    $VMGUI "$VM" type "tier5/foot"
    sleep 1
}
```

## Steps

**Readiness is mechanical — run every `await_*` line as written.** A waiter that
TIMES OUT is that step's failure; keep the frame as evidence. The password
dialog is a separate window titled `Authentication Required`; on qdwin a newly
mapped prompt may not receive keyboard focus, so the steps CLICK into it
rather than typing blind.

### S1 — first launch: the password dialog; Cancel launches nothing

```bash
mark s1
launch_foot_vm
qdwin_screenshot "$ART/63-s1-launcher.png"
```

OPEN `63-s1-launcher.png`. The launcher lists the guest's foot as a VM
catalogue row (appId `tier5/foot`): **Foot** with a `[VM]` description when
the boot-time scan published the guest base's catalogue, or **Foot Terminal
(VM)** when VMApps fell back to its built-in row. Either is the same launch
(`--policy-key tier5/foot -- foot`); the guest base ships foot either way.
It must be the ONLY row ("1 result") — the `tier5/foot` filter guarantees
that; any other row is a setup ERROR, not something to navigate around.

Start it with exactly ONE activation: the Return below. Do not also click
the row — a click launches it as well, and the following Return then lands
in the freshly focused password dialog and submits an empty password (the
agent logs `-> deny (empty-password)` and the dialog is gone before the
screenshot):

```bash
$VMGUI "$VM" key Return
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; c=$(cat /run/qci-63/s1.cur); await_journal_line_after_cursor "$c" "polkit BeginAuth: action=org.qdistro.tier5.bridge method=pam\$" 60 1 -u qdistro-polkit-agent.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_qdwin_window_title "Authentication Required" 30'
qdwin_screenshot "$ART/63-s1-password-dialog.png"
# Nothing may have answered the dialog yet (a stray key would show here).
$VMEXEC "$VM" 'c=$(cat /run/qci-63/s1.cur); echo "s1-verdicts=$(journalctl -u qdistro-polkit-agent.service --after-cursor "$c" --no-pager -o cat | grep -c "action=org.qdistro.tier5.bridge method=pam -> ")"'
# Ground truth while the dialog is up: the wrapper was started BY qdshell, and
# nothing privileged runs yet — no bridge, no guest.
$VMEXEC "$VM" 'bash /run/qci-63/probe.sh wrapper; bash /run/qci-63/probe.sh counts'
```

**Assert (S1, dialog):**
- The agent logged `polkit BeginAuth: action=org.qdistro.tier5.bridge method=pam`
  (method `pam`, never `broker`).
- `s1-verdicts=0`: the dialog was still unanswered when captured.
- OPEN `63-s1-password-dialog.png`: an "Authentication Required" dialog with the
  message "Authentication required to show an isolated VM app on your
  desktop", an `Action: org.qdistro.tier5.bridge` line, a `Password:` field
  and OK/Cancel buttons. No foot window is on screen.
- `wrapper=<pid>` is non-empty and its parent is qdshell's `qs` process;
  `bridges=0` — no uid-0 bridge exists before authentication — with
  `pkexec-pending=1` (the setuid pkexec waiting on the dialog: its listed
  line shows `ruid=1000 euid=0 exe=…/pkexec`), `bridge-other=0`,
  `defined=0`, `qemu=0`, `records=0`.

Click the dialog's **Cancel** button using coordinates read from that frame
(`qdwin_mouse_move X Y`, `sleep 0.5`, `qdwin_click X Y`). Then:

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; c=$(cat /run/qci-63/s1.cur); await_journal_line_after_cursor "$c" "action=org.qdistro.tier5.bridge method=pam -> deny \(prompt-cancelled\)" 30 1 -u qdistro-polkit-agent.service'
# The wrapper gives up (bridge never started) and exits.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; c=$(cat /run/qci-63/s1.cur); await_journal_line_after_cursor "$c" "tier-5 bridge did not start the waypipe client" 60 1 _UID=1000'
$VMEXEC "$VM" 'bash /run/qci-63/probe.sh wait-wrappers 0; bash /run/qci-63/probe.sh counts'
qdwin_screenshot "$ART/63-s1-cancelled.png"
```

**Assert (S1, cancel):**
- The agent logged `-> deny (prompt-cancelled)` for the action.
- The wrapper logged (through qdshell's journal) that the bridge did not start.
- `wrappers=0`, `bridges=0`, `pkexec-pending=0`, `defined=0`, `qemu=0`, `overlay=0`, `records=0`,
  `new-waypipe=0`.
- OPEN `63-s1-cancelled.png`: no dialog, no foot window.

### S2 — launch again, type the admin password: the app runs, tagged and registered

```bash
mark s2
launch_foot_vm
$VMGUI "$VM" key Return
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_qdwin_window_title "Authentication Required" 60'
qdwin_screenshot "$ART/63-s2-password-dialog-again.png"
```

(As in S1: one activation only — the Return above; never click the row too.)
Click into the **Password:** field using coordinates read from that frame
(`qdwin_mouse_move X Y`, `sleep 0.5`, `qdwin_click X Y`), then type
`Pa_ssw0rd45` and press Enter:

```bash
qdwin_chord shift -- p
qdwin_type_lower a
qdwin_chord shift -- minus
qdwin_type_lower ssw0rd45
qdwin_send_key KEY_ENTER
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; c=$(cat /run/qci-63/s2.cur); await_journal_line_after_cursor "$c" "action=org.qdistro.tier5.bridge method=pam -> allow \(pam-ok\)" 60 1 -u qdistro-polkit-agent.service'
# The bridge reports READY only after the waypipe client is verified and
# RegisterLaunch succeeded — BEFORE the guest boots.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; c=$(cat /run/qci-63/s2.cur); await_journal_line_after_cursor "$c" "bridge ready: waypipe client pid=[0-9]+ silo=vm-t5-tier5-foot-" 60 1 _UID=1000'
$VMEXEC "$VM" 'journalctl -n0 --show-cursor 2>/dev/null | sed -n "s/^-- cursor: //p" > /run/qci-63/s3.cur'
```

If the agent logged `-> deny` with a PAM reason instead (wrong characters
typed), that is a harness typing failure, not a product verdict: record ERROR
with the agent line, do not retry more than once.

Go to S3 without waiting for the guest to boot; S2's remaining assertions
are checked after S3's launch.

### S3 — a second launch asks for the password again

```bash
launch_foot_vm
$VMGUI "$VM" key Return
# The second wrapper is up (started by qdshell) ...
$VMEXEC "$VM" 'bash /run/qci-63/probe.sh wait-wrappers 2'
# ... and its bridge became ready (or a dialog appeared). _UID=1000 covers
# qdshell's spawn output AND the agent (User=admin), never qemu-ga's own log.
# ... and the agent was asked again: a fresh wrapper is a fresh polkit
# subject. _UID=1000 covers qdshell's spawn output AND the agent (User=admin).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; c=$(cat /run/qci-63/s3.cur); await_journal_line_after_cursor "$c" "bridge ready: waypipe client pid=[0-9]+ silo=vm-t5-tier5-foot-|polkit BeginAuth: action=org.qdistro.tier5.bridge" 60 1 _UID=1000'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_qdwin_window_title "Authentication Required" 60'
qdwin_screenshot "$ART/63-s3-second-launch.png"
```

Click into the dialog's **Password:** field (coordinates from that frame) and
type the password again:

```bash
qdwin_chord shift -- p
qdwin_type_lower a
qdwin_chord shift -- minus
qdwin_type_lower ssw0rd45
qdwin_send_key KEY_ENTER
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; c=$(cat /run/qci-63/s3.cur); await_journal_line_after_cursor "$c" "action=org.qdistro.tier5.bridge method=pam -> allow \(pam-ok\)" 60 1 -u qdistro-polkit-agent.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; c=$(cat /run/qci-63/s3.cur); await_journal_line_after_cursor "$c" "bridge ready: waypipe client pid=[0-9]+ silo=vm-t5-tier5-foot-" 60 1 _UID=1000'
$VMEXEC "$VM" 'c=$(cat /run/qci-63/s3.cur); echo "s3-beginauth=$(journalctl -u qdistro-polkit-agent.service --after-cursor "$c" --no-pager -o cat | grep -c "BeginAuth: action=org.qdistro.tier5.bridge method=pam\$")"; echo "s3-pamok=$(journalctl -u qdistro-polkit-agent.service --after-cursor "$c" --no-pager -o cat | grep -c "action=org.qdistro.tier5.bridge method=pam -> allow (pam-ok)")"; echo "s3-ready=$(journalctl _UID=1000 --after-cursor "$c" --no-pager -o cat | grep -c "bridge ready: waypipe client pid=")"'
```

As in S2, a `-> deny` with a PAM reason here is a harness typing failure:
record ERROR with the agent line, retry the typing at most once.

**Assert (S3):**
- `wrappers=2` (both sessions running).
- OPEN `63-s3-second-launch.png`: the same "Authentication Required" dialog
  as in S1/S2, for `Action: org.qdistro.tier5.bridge`. No dialog here (the
  second launch admitted without authenticating) is a FAIL.
- `s3-beginauth=1` and `s3-pamok=1`: the agent asked once more and PAM
  verified the password; `s3-ready=1`: only then did the second bridge come
  up.

### S2/S3 (continued) — the guest boots; windows are tagged and registered

Wait for as many tagged clients and windows as launches whose bridge came
up since S2 (`launches=`, 2 when S2 and S3 both authenticated). The waiters'
third argument is a poll interval, not a count, so the count is checked
explicitly: a failed S3 must not stall the wait, and S3 must not be graded
on the first window alone.

```bash
$VMEXEC "$VM" 'c=$(cat /run/qci-63/s2.cur); n=$(journalctl _UID=1000 --after-cursor "$c" --no-pager -o cat | grep -c "bridge ready: waypipe client pid="); echo "$n" > /run/qci-63/launches; echo "launches=$n"'
# Up to ~6 min for the nested cold boot + publisher.
$VMEXEC "$VM" 'N=$(cat /run/qci-63/vmname); n=$(cat /run/qci-63/launches); c=$(cat /run/qci-63/s2.cur); for _ in $(seq 1 180); do a=$(journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service --after-cursor "$c" --no-pager -o cat | grep -c "qdwin/secctx: client accepted engine=qdistro.tier5 app_id=qdistro.tier5.$N instance_id="); [ "$a" -ge "$n" ] && break; sleep 2; done; echo "accepted-wait=$a/$n"; [ "$a" -ge "$n" ]'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; N=$(cat /run/qci-63/vmname); n=$(cat /run/qci-63/launches); for _ in $(seq 1 60); do w=$(qdwin_windows 2>/dev/null | awk -F"\t" -v p="^\\[tier5:$N\\] " "\$5 ~ p" | wc -l); [ "$w" -ge "$n" ] && break; sleep 2; done; echo "windows-wait=$w/$n"; [ "$w" -ge "$n" ]'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; qdwin_windows'
$VMEXEC "$VM" 'bash /run/qci-63/probe.sh accepted s2; bash /run/qci-63/probe.sh counts; bash /run/qci-63/probe.sh rows'
qdwin_screenshot "$ART/63-s2-app-running.png"
```

**Assert (S2):**
- The agent logged `-> allow (pam-ok)` for the action (S2's password).
- OPEN `63-s2-password-dialog-again.png`: the same dialog as S1 appeared
  again (the cancelled attempt left no grant behind).
- The compositor logged `client accepted engine=qdistro.tier5
  app_id=qdistro.tier5.<VMNAME>`; the window list has a toplevel titled
  `[tier5:<VMNAME>] …`.
- `launches=2` and `records=2`; each printed register row reads
  `engine='qdistro.tier5' app='qdistro.tier5.<VMNAME>'`; `running=1`,
  `qemu=1` (one shared guest), `bridges=2` with each `bridge` line showing
  `ruid=0` and a python exe, `pkexec-pending=0`.
- OPEN `63-s2-app-running.png`: a foot terminal window is visible at full
  opacity, no dialog.
- (S3) a SECOND foot window is listed and visible (`accepted=2`,
  `windows-wait=2/2`) — the second, separately authenticated session joined
  the same guest.

### S4 — close: guest, overlay and every chain process are gone

```bash
# What the window close does: SIGTERM each wrapper; the last one reaps.
$VMEXEC "$VM" 'N=$(cat /run/qci-63/vmname); pkill -TERM -u admin -f "[s]pawn-tier5.sh --vm $N"; true'
$VMEXEC "$VM" 'bash /run/qci-63/probe.sh wait-reaped; bash /run/qci-63/probe.sh counts'
qdwin_screenshot "$ART/63-s4-closed.png"
```

**Assert (S4):**
- `wrappers=0`, `bridges=0`, `pkexec-pending=0`, `defined=0`, `qemu=0`, `overlay=0`,
  `new-waypipe=0`, `root-files=0`.
- OPEN `63-s4-closed.png`: no foot window and no dialog remain.

## Teardown

```bash
$VMEXEC "$VM" 'N=$(cat /run/qci-63/vmname 2>/dev/null); pkill -KILL -u admin -f "[s]pawn-tier5.sh --vm t5-tier5-foot-" 2>/dev/null; [ -n "$N" ] && runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 HOME=/home/admin /usr/local/bin/qdistro-tier5-cleanup "$N"; true'
$VMEXEC "$VM" 'systemctl stop qci-63-login.service 2>/dev/null; rm -f /etc/polkit-1/rules.d/11-qci-63-tier5-bridge-auth.rules; faillock --user admin --reset 2>/dev/null; rm -rf /run/qci-63; true'
```

## Screenshots
- `63-s1-launcher.png` — the launcher with the foot VM row (`tier5/foot`)
- `63-s1-password-dialog.png` — the agent's password dialog for the tier-5 bridge
- `63-s1-cancelled.png` — after Cancel: no dialog, no app window
- `63-s2-password-dialog-again.png` — the dialog again on the second launch
- `63-s3-second-launch.png` — the second launch's password dialog (expected)
- `63-s2-app-running.png` — the foot window(s) from the tier-5 guest
- `63-s4-closed.png` — after closing: nothing left on screen
