# 58 — tier-3s sandboxed window renders on the desktop and takes input

<!-- qci:visual: required -->

**What**: launch a tier-3s (gVisor systrap) silo whose workload is
`weston-terminal`, verify the sandboxed toplevel reaches the qdwin
desktop through the waypipe bridge with the `[3s:<silo>]` title prefix,
focus it, type into it with the KVM keyboard, and read the sandbox's own
identity (`uid`, container hostname) off the painted frame — proving the
input path reaches *inside* the sandbox and the rendered output comes
back through the same bridge.

**Why**: `tests/integration/vm/s123-tier3s-waypipe.sh` proves the wire
end-to-end (bridge pair, secctx tagging, control record, single-attach)
from journal and `podman inspect` evidence, but never asserts the
*painted frame* — a runner-level check that an actual user-facing window
exists and reacts to input. This scenario is that corroboration: an
agent looks at the desktop the way a user would.

Tier-3s is dev-profile only and `network=none` — no base image, no
nested KVM. The substrate is provisioned into the running GUI worker by
`tests/integration/vm/tier3s-gui-provision.sh` (installer, pinned runsc,
workload OCI archives, dev profile stamp, qdwin session up). A present
but broken tier-3s install is a real FAIL, not a skip.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM (these scenarios are driven with an explicit VM)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}/58
mkdir -p "$ART"
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "$VM"
```

Precondition: this is a qdwin-lane VM — the product session (qdwin
compositor + qdshell) owns `wayland-1`.

```bash
$VMEXEC "$VM" 'runuser -u admin -- test -S /run/user/1000/wayland-1' \
    || { echo "ERROR: no wayland-1 — not a qdwin-lane VM"; exit 2; }
```

Provision the tier-3s stack into this worker (idempotent; fails loudly
on any missing pinned input):

```bash
$QDISTRO_REPO/tests/integration/vm/tier3s-gui-provision.sh "$VM" weston-terminal \
    || { echo "ERROR: tier-3s provisioning failed"; exit 2; }
```

Drain any leftover silo/bridge state from a prior run:

```bash
B64=$(base64 -w0 <<'EOF'
source /var/tmp/t3s-dl/tier3s-guest-lib.sh
for s in t3sgui t3scls; do
    [ "$(silo_state "$s")" = absent ] && continue
    sm StopSilo si "$s" 10 >/dev/null 2>&1 || :
    sm DeleteSilo s "$s" >/dev/null 2>&1 || :
done
set_rules none
finish
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Steps

### S1 — create the silo, allow the spawn, launch

All guest work goes through the staged `tier3s-guest-lib.sh` — the same
helpers the bats lanes use. The silo is named `t3sgui`, workload
`weston-terminal`, app id `qdistro.tier3s.t3sgui`, network `none`.

```bash
B64=$(base64 -w0 <<'EOF'
source /var/tmp/t3s-dl/tier3s-guest-lib.sh
D=/tmp/t3s-58.d; mkdir -p "$D"
SILO=t3sgui
GUISPAWN="qdistro.tier3s.spawn:weston-terminal/weston-terminal"
sm CreateTier3sSilo ssss "$SILO" weston-terminal "$SILO" none >/dev/null
[ "$(silo_state "$SILO")" = Created ] || { echo "FAIL: silo not Created"; exit 1; }
set_rules "allow:$GUISPAWN"
[ "$(broker_check "$GUISPAWN")" = allow ] || { echo "FAIL: broker did not allow"; exit 1; }
journal_cursor > "$D/journal.cur"
TOK=$(up_gui_silo "$SILO")
[ -n "$TOK" ] || { echo "FAIL: launch did not come up"; exit 1; }
echo "TOK=$TOK" > "$D/tok"
t3s_window_handle "$SILO" "$(cat "$D/journal.cur")" > "$D/handle"
echo "silo up: token=$TOK handle=$(cat "$D/handle") uid=$(silo_uid "$SILO")"
finish
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: the guest script exits 0 and prints `silo up: token=<32 hex>
handle=<N> uid=<silo-uid>`. `up_gui_silo` returns only once qdshell's
journal logged `[tier3s] toplevel observed silo=t3sgui` — so a returned
token already means the compositor saw the bridged surface. The uid must
be != 1000 (a distinct `qt3s-t3sgui` account).

### S2 — journal evidence for this launch only

Scope every grep to the cursor captured just before the launch.

```bash
B64=$(base64 -w0 <<'EOF'
source /var/tmp/t3s-dl/tier3s-guest-lib.sh
D=/tmp/t3s-58.d
cur=$(cat "$D/journal.cur"); TOK=$(sed -n 's/^TOK=//p' "$D/tok")
SILO=t3sgui; APPID="qdistro.tier3s.$SILO"
[ "$(qdshell_log "$cur" | grep -c "\[tier3s\] toplevel observed silo=$SILO secctx=$APPID color=#...... handle=[0-9]")" = 1 ] \
    || { echo "FAIL: no tier3s toplevel-observed line"; qdshell_log "$cur" | tail -20; exit 1; }
[ "$(comp_log "$cur" | grep -c "toplevel_\(added\|title\) .*title=\"\[3s:$SILO\] ")" -ge 1 ] \
    || { echo "FAIL: no [3s:] title prefix in compositor journal"; exit 1; }
[ "$(comp_log "$cur" | grep -c "toplevel_security_context handle=[0-9]* engine=qdistro.tier3s app_id=$APPID instance=$TOK")" = 1 ] \
    || { echo "FAIL: no secctx line with launch token"; exit 1; }
echo "journal: toplevel observed, [3s:$SILO] title, secctx engine=qdistro.tier3s instance=$TOK"
finish
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: exit 0 — qdshell observed the toplevel with the secctx app
id, the compositor assigned the `[3s:t3sgui] ` title prefix, and the
security-context line carries this launch's token.

### S3 — the window is actually painted

```bash
qdwin_screenshot "$ART/s3-window.png"
```

**Assert (visual)**: open `$ART/s3-window.png`. It shows a normal
application window — a terminal with a dark content area — on the qdwin
desktop. The window's title (in qdshell's chrome) begins with
`[3s:t3sgui]`. This is the load-bearing visual assertion: a user sitting
at this desktop sees an ordinary window, not a broken or absent surface.

### S4 — keystrokes reach inside the sandbox

Focus the silo window, then type two commands on the KVM keyboard and
read the answers off the screen.

`qdwin_focus_window` is guest-side (`/tmp/qci-gui-waiters.sh`); the
typing helpers are host-side QMP:

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "\\[3s:t3sgui\\].*"' \
    || { echo "FAIL: silo window not focusable"; exit 1; }
qdwin_type_lower 'id'
qdwin_send_key KEY_ENTER
qdwin_type_lower 'uname -n'
qdwin_send_key KEY_ENTER
qdwin_type_lower 'echo t3s-visible-ok'
qdwin_send_key KEY_ENTER
qdwin_screenshot "$ART/s4-typed.png"
```

**Assert (visual + structured)**: open `$ART/s4-typed.png`. The terminal
shows the three commands and their output:

- `id` prints `uid=<N>(qt3s-t3sgui)` where N equals the silo uid printed
  at the end of S1 (read it from that transcript line, not from
  memory) — the sandbox runs as the per-silo account, not admin.
- `uname -n` prints the container's hostname — a 12-char hex podman ID
  prefix, plainly NOT the VM's own hostname (the VM's is its clone
  name; cross-check with `hostname` on the host-side `vm-exec`).
- `t3s-visible-ok` is echoed back.

That proves host input travelled compositor → waypipe client →
`link.sock` → waypipe server → the sandboxed client, and the rendered
output made the return trip — a real interactive channel, not just a
mapped surface.

If the frame shows nothing typed, capture a second `qdwin_screenshot`
once to rule out a slow repaint; a still-empty terminal is a FAIL
(input path broken), not a flake to absorb.

### S5 — network=none is visible inside the sandbox

Under `network=none` the sandbox's only interface is `lo`; gVisor does
not even mount `/sys/class/net` (`ls` there fails) — `/proc/net/dev`
is the in-sandbox source of truth. Type it, read the frame, then take
the structured cross-check.

```bash
qdwin_type_lower 'cat '
qdwin_send_key KEY_SLASH
qdwin_type_lower 'proc'
qdwin_send_key KEY_SLASH
qdwin_type_lower 'net'
qdwin_send_key KEY_SLASH
qdwin_type_lower 'dev'
qdwin_send_key KEY_ENTER
qdwin_screenshot "$ART/s5-netnone.png"
```

**Assert (visual)**: the `/proc/net/dev` table lists exactly one
interface, `lo` — no `eth0`, no `wlan*`, no sit/tun.

Structured cross-check (same fact, authoritative):

```bash
B64=$(base64 -w0 <<'EOF'
source /var/tmp/t3s-dl/tier3s-guest-lib.sh
SILO=t3sgui; CTR=$(ctr_of "$SILO")
pm_s "$SILO" exec "$CTR" cat /proc/net/dev
pm_s "$SILO" inspect --format '{{.HostConfig.NetworkMode}}' "$CTR"
finish
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: the transcript's interface table names `lo` and nothing
else, then prints `none`.

### S6 — stop the silo; the window leaves

```bash
B64=$(base64 -w0 <<'EOF'
source /var/tmp/t3s-dl/tier3s-guest-lib.sh
D=/tmp/t3s-58.d
SILO=t3sgui; TOK=$(sed -n 's/^TOK=//p' "$D/tok"); UNIT=$(unit_of "$SILO")
H=$(cat "$D/handle")
[ -n "$H" ] || { echo "FAIL: no handle recorded in S1"; exit 1; }
cur=$(journal_cursor)
sm StopSilo si "$SILO" 10 >/dev/null
[ "$(silo_state "$SILO")" = Stopped ] || { echo "FAIL: silo not Stopped"; exit 1; }
wait_for 90 unit_down "$UNIT" || { echo "FAIL: unit still up"; exit 1; }
wait_for 30 bash -c "journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service --no-pager -o cat --after-cursor='$cur' | grep -q 'toplevel_removed handle=$H'" \
    || { echo "FAIL: no toplevel_removed for handle $H"; exit 1; }
assert_launch_gone teardown "$TOK" "$SILO"
assert_bridge_gone teardown "$TOK"
echo "teardown: unit down, toplevel_removed seen, launch+bridge gone"
finish
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
qdwin_screenshot "$ART/s6-gone.png"
```

**Assert**: the guest script exits 0 — the silo is Stopped, the launch
unit is down, the compositor logged `toplevel_removed` for the recorded
handle, and both `assert_launch_gone`/`assert_bridge_gone` pass (scope,
container, runsc state, bridge pids, launch record all reaped). Open
`$ART/s6-gone.png`: the `[3s:t3sgui]` window is gone from the desktop.

## Teardown

```bash
B64=$(base64 -w0 <<'EOF'
source /var/tmp/t3s-dl/tier3s-guest-lib.sh
SILO=t3sgui
sm StopSilo si "$SILO" 10 >/dev/null 2>&1 || :
sm DeleteSilo s "$SILO" >/dev/null 2>&1 || :
set_rules none
finish
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Known caveats

- **This is a qdwin-lane scenario.** Without the product session there
  is nothing to render into; `gui_scenario_skip_reason` skips it only
  when the qdwin/qdshell stack is genuinely absent.
- **Provisioning is per-VM, not per-golden.** `tier3s-gui-provision.sh`
  needs the pinned runsc tarball and workload OCI archives in the host
  cache (`~/.cache/qdistro/`); a missing pin is an ERROR, never a skip.
- **Input goes through the compositor's focus.** `qdwin_focus_window`
  must report focus before typing; do not drive the window by pixel
  click.
- **The container hostname is the assertion.** `uname -n` inside the
  sandbox prints podman's 12-char container-ID hostname — distinct from
  the guest's own; the screenshot must show a value that is NOT the VM
  hostname.
