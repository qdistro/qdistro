# 19 — tier-5 loopback toplevel renders with secctx chrome

**What**: spawn `weston-terminal` via `spawn-tier5.sh --loopback` and
verify the resulting toplevel reaches the outer qdshell with the
correct title prefix (`[tier5:loopback-…]`) and secctx-tagged
window-chrome treatment.

**Why**: this is the visual half of the tier-5 loopback contract.
`tests/integration/vm/s43-tier5-loopback.sh` covers the wire (vsock
listener up, waypipe halves running, weston-terminal process alive).
Here we corroborate visually that the forwarded toplevel renders as
a normal app window with the tier-5 title-prefix badge.

Loopback uses `vsock_loopback` (CID=1) — no real guest VM is started,
so this scenario does **not** require nested KVM or the
`qdistro-tier5-base.qcow2` image. It exercises the data path only.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM (these scenarios are driven with an explicit VM)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

# Precondition: outer compositor + qdshell up.
$VMEXEC "$VM" 'runuser -u admin -- test -S /run/user/1000/wayland-1'
$VMEXEC "$VM" 'runuser -u admin -- pgrep -af "[q]s -p" >/dev/null'

# Precondition: tier-5 source unpacked + waypipe + weston-terminal
# present in the VM. The bats s35 driver bootstraps the same tree.
$VMEXEC "$VM" 'command -v waypipe >/dev/null && command -v weston-terminal >/dev/null'
$VMEXEC "$VM" 'test -d /root/qdistro-src/qdistro/tier5-vm'

# Drain any leftover tier-5 spawn from a prior run. ([s]pawn bracket trick so
# the pkill pattern can't match the guest shell running this very command.)
$VMEXEC "$VM" 'pkill -u root -f "[s]pawn-tier5.sh" 2>/dev/null || true; \
               pkill -u admin -f "[w]aypipe.*vsock.*1:" 2>/dev/null || true; \
               sleep 1'
```

## Steps

### S1 — spawn tier-5 loopback weston-terminal

`spawn-tier5.sh` sources its helper via `$(dirname)/../lib/spawn-common.sh`,
so `lib/` must be staged as a **sibling** of `tier5-vm/` and the script run
from inside the copied `tier5-vm/`. (Mirrors `21-tier5-close-cleanup.md`'s
staging; a flat copy of `tier5-vm` alone leaves `../lib` unresolved, so
`gen_launch_token`/`qd_register_secctx_launch_record` go undefined and the
launch token + lineage registration are silently skipped.)

```bash
B64=$(base64 -w0 <<'EOF'
rm -rf /tmp/qdistro-tier5
mkdir -p /tmp/qdistro-tier5
cp -r /root/qdistro-src/qdistro/tier5-vm /tmp/qdistro-tier5/tier5-vm
cp -r /root/qdistro-src/qdistro/lib /tmp/qdistro-tier5/lib
chmod -R a+rX /tmp/qdistro-tier5
find /tmp/qdistro-tier5 -name '*.sh' -exec chmod a+rx {} +
setsid bash /tmp/qdistro-tier5/tier5-vm/spawn-tier5.sh --loopback -p 7791 \
    -- weston-terminal </dev/null >/tmp/s19-spawn.log 2>&1 &
disown
sleep 3
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Verify** (journal-side): listener ready breadcrumb.
```bash
$VMEXEC "$VM" 'grep "vsock listener ready cid=1 port=7791" /tmp/s19-spawn.log'
```
Must print one matching line.

**Verify** (staging guard): the `spawn-common.sh` helper sourced cleanly —
no missing-sibling-`lib/` errors that would silently drop the launch token.
```bash
$VMEXEC "$VM" '! grep -qE "spawn-common\.sh: No such file|gen_launch_token: command not found|qd_register_secctx_launch_record: command not found" /tmp/s19-spawn.log'
```
Must exit 0 (no such errors present).

### S2 — tier-5 toplevel reaches qdshell with the tier5 title prefix

The **authoritative** check is qdwin's own journal. When the forwarded
weston-terminal toplevel reaches the outer compositor, qdwin logs the
title it assigned — which carries the tier-5 prefix. Screenshots taken
over the SPICE/QXL framebuffer can show **stale** window chrome (a frame
captured before the title repaint), so the journal — not the pixels — is
ground truth here.

Wait for the inner weston-terminal to map (~2s), then read the journal.

```bash
sleep 2
# Authoritative: qdwin received the forwarded toplevel and assigned it the
# [tier5:loopback-<pid>] title prefix. The <pid> suffix varies — match the
# prefix only. Wide --since window (this is a fresh disposable VM with a single
# tier-5 spawn, so a stale match is impossible) tolerates a slow agent pausing
# between the S1 map and this assertion.
$VMEXEC "$VM" "journalctl --since '10min ago' | grep 'qdwin: toplevel_title' | grep -F '[tier5:loopback-' | head"
```

**Assert** (deterministic): the command prints at least one line — qdwin
saw the forwarded toplevel and tagged it `[tier5:loopback-<pid>]`. This
proves the loopback data path end-to-end (vsock → waypipe client →
waypipe server → outer compositor) **and** the secctx title-prefix
treatment, without depending on framebuffer freshness.

Corroborating screenshot (best-effort, **NOT** load-bearing):

```bash
$VMGUI "$VM" screenshot /tmp/s19-loopback-toplevel.png
```

Note in the report whether the `[tier5:loopback-` prefix is also visible
in the window/taskbar chrome (and that a weston-terminal window with a
dark background + top-left text cursor is shown). Do **not** FAIL on a
stale or prefix-less screenshot when the journal assertion above passed —
the journal is the ground truth.

### S3 — click into the terminal; verify input forwarded

Position click at the centre of the terminal's content area.

```bash
# OCR-find the terminal: the cursor at top-left is at ~ (textCol, textRow);
# click somewhere safely inside the terminal client area.
# (Runner: read the screenshot, locate the terminal window's bounding
# box, click its centre.)
$VMGUI "$VM" click <cx> <cy>
sleep 0.3
$VMEXEC "$VM" 'runuser -u admin -- ydotool type "echo tier5-ok"'
sleep 0.2
$VMEXEC "$VM" 'runuser -u admin -- ydotool key enter'
sleep 0.5
$VMGUI "$VM" screenshot /tmp/s19-after-type.png
```

**Assert** (agent-visual): the second screenshot shows the typed
characters `tier5-ok` echoed on a new line inside the terminal.
This confirms the input event made it from outer-host →
waypipe-client → vsock → waypipe-server → inner weston-terminal
and the rendered frame came back across the same path.

If `ydotool` isn't installed (`uinput` kernel module missing — see
`todo/qdwin-vm/ydotool-install-uinput-missing.md`), this step is a
**soft pass** — note the input-injection limitation and move on.
Don't FAIL on it.

### S4 — journal cross-check (load-bearing)

```bash
$VMEXEC "$VM" "journalctl --since '1min ago' | grep -E 'qdwin:.*tier5|tier5.*qdwin' | head -20"
```

**Assert**: at least one journal line indicating qdwin saw the
tier-5-tagged client or assigned chrome to its toplevel. This overlaps
S2's authoritative `toplevel_title` check; if this particular grep
matches nothing but S2 passed, note it as a logging gap (file as a
follow-up) and do **not** FAIL — S2's journal assertion is the
deterministic ground-truth here.

### S5 — cleanup

```bash
$VMEXEC "$VM" 'pkill -u root -f "[s]pawn-tier5.sh.*loopback" 2>/dev/null || true; \
               sleep 1; \
               pkill -u root -f "[s]pawn-tier5.sh.*loopback" 2>/dev/null || true'
```

## Known caveats

- **ydotool may be soft-passed.** See
  `todo/qdwin-vm/ydotool-install-uinput-missing.md` — `uinput` kernel
  module isn't in the baseweed kernel. Until rebuilt with `CONFIG_INPUT_UINPUT=m`,
  S3 input injection is best-effort.
- **No tier-5 silo-badge yet.** `doc/ui.md` "silo-badges" reserves a
  distinct colour ring for tier-5, but the qdshell side
  (`Services/Qdistro/VMApps.qml`) doesn't exist on main as of
  2026-05-15. Until it lands, the title-prefix is the only visible
  badge. Update this scenario to assert badge ring colour once
  VMApps.qml ships.
- **Loopback ≠ isolation.** This scenario exercises only the wire.
  The actual tier-5 security boundary is the `--vm` path; see
  `20-tier5-vm-cold-start.md`.
