# 59 — closing the tier-3s window unwinds the whole launch

<!-- qci:visual: required -->

**What**: launch a tier-3s `weston-terminal` silo, close the sandboxed
application from *inside* it (type `exit` at its shell — the user's own
gesture, not an admin StopSilo), and verify the whole launch unwinds:
toplevel removed, launch unit down, scope/container/bridge/launch-record
reaped — then launch the same silo again to prove the path is not a
one-shot.

**Why**: `tests/integration/vm/s124-tier3s-app.sh` and s125 cover
stop-side cleanup under an explicit `StopSilo`. This scenario exercises
the other direction a user actually hits: the sandboxed process exits on
its own (shell `exit`, close button) and everything behind it — the
waypipe server, the container, the bridge client, the launch unit, the
control record — must drain without an explicit stop. A leftover scope
or a wedged launch dir is exactly the kind of residue that only shows
up when the *app* initiates teardown.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM (these scenarios are driven with an explicit VM)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}/59
mkdir -p "$ART"
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "$VM"
```

Precondition: qdwin-lane VM (product session owns `wayland-1`).

```bash
$VMEXEC "$VM" 'runuser -u admin -- test -S /run/user/1000/wayland-1' \
    || { echo "ERROR: no wayland-1 — not a qdwin-lane VM"; exit 2; }
```

Provision tier-3s (idempotent):

```bash
$QDISTRO_REPO/tests/integration/vm/tier3s-gui-provision.sh "$VM" weston-terminal \
    || { echo "ERROR: tier-3s provisioning failed"; exit 2; }
```

Drain leftovers:

```bash
B64=$(base64 -w0 <<'EOF'
source /var/tmp/t3s-dl/tier3s-guest-lib.sh
s=t3scls
[ "$(silo_state "$s")" = absent ] || {
    sm StopSilo si "$s" 10 >/dev/null 2>&1 || :
    sm DeleteSilo s "$s" >/dev/null 2>&1 || :
}
set_rules none
finish
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Steps

### S1 — launch the silo

```bash
B64=$(base64 -w0 <<'EOF'
source /var/tmp/t3s-dl/tier3s-guest-lib.sh
SILO=t3scls
GUISPAWN="qdistro.tier3s.spawn:weston-terminal/weston-terminal"
sm CreateTier3sSilo ssss "$SILO" weston-terminal "$SILO" none >/dev/null
[ "$(silo_state "$SILO")" = Created ] || { echo "FAIL: silo not Created"; exit 1; }
set_rules "allow:$GUISPAWN"
journal_cursor > /tmp/s59-journal.cur
TOK=$(up_gui_silo "$SILO")
[ -n "$TOK" ] || { echo "FAIL: launch did not come up"; exit 1; }
echo "TOK=$TOK" > /tmp/s59-tok
t3s_window_handle "$SILO" > /tmp/s59-handle
snapshot_launch "$TOK"; snapshot_bridge "$TOK"
echo "silo up: token=$TOK handle=$(cat /tmp/s59-handle)"
finish
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: exit 0 and a `token=`/`handle=` line.

### S2 — the window is on screen

```bash
qdwin_screenshot "$ART/s2-window.png"
```

**Assert (visual)**: a terminal window titled `[3s:t3scls] …` is
present on the desktop.

### S3 — the app exits; everything downstream unwinds

Type `exit` into the sandboxed shell — the window closes because the
*application* ended, the way a user closes a terminal.

`qdwin_focus_window` is guest-side (`/tmp/qci-gui-waiters.sh`); typing
is host-side QMP:

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "\\[3s:t3scls\\].*"' \
    || { echo "FAIL: silo window not focusable"; exit 1; }
qdwin_type_lower 'exit'
qdwin_send_key KEY_ENTER
```

Now assert the unwind from the guest's own ground truth — bounded waits,
not sleeps: the compositor must log `toplevel_removed` for the recorded
handle, the launch unit must end, and every launch artifact must be
gone.

```bash
B64=$(base64 -w0 <<'EOF'
source /var/tmp/t3s-dl/tier3s-guest-lib.sh
SILO=t3scls; UNIT=$(unit_of "$SILO")
TOK=$(sed -n 's/^TOK=//p' /tmp/s59-tok); H=$(cat /tmp/s59-handle)
[ -n "$TOK" ] && [ -n "$H" ] || { echo "FAIL: missing token/handle from S1"; exit 1; }
# the app exiting must drop its toplevel without an explicit StopSilo
wait_for 60 bash -c "journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service --no-pager -o cat | grep -q 'toplevel_removed handle=$H'" \
    || { echo "FAIL: no toplevel_removed for handle $H"; comp_log | tail -15; exit 1; }
# the launch unit follows the container exit
wait_for 90 unit_down "$UNIT" || { echo "FAIL: $UNIT still up after app exit"; exit 1; }
# Nominal state stays Active — it is user intent, not runtime truth
# (session_manager observes but never rewrites it). The checkable
# contract is the observer thread's evidence: it must report `stopped`
# once the launcher is inactive AND the container is gone (probed every
# ~10 s; allow for teardown + one extra cycle). NB: wait_for calls its
# command in a subshell — lib functions are visible, `bash -c` is not.
obs_stopped() { silo_observed "$1" | grep -q '^stopped'; }
wait_for 60 obs_stopped "$SILO" \
    || { echo "FAIL: observed_status not stopped; last=$(silo_observed "$SILO")"; exit 1; }
[ "$(silo_state "$SILO")" = Active ] || { echo "FAIL: nominal state moved; got $(silo_state "$SILO")"; exit 1; }
assert_launch_gone app-exit "$TOK" "$SILO"
assert_bridge_gone app-exit "$TOK"
echo "app-exit unwind: toplevel_removed, unit down, observed=$(silo_observed "$SILO"), launch+bridge gone"
finish
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
qdwin_screenshot "$ART/s3-gone.png"
```

**Assert**: the guest script exits 0 (its own fail lines name the stuck
artifact when it does not). Open `$ART/s3-gone.png`: the `[3s:t3scls]`
window is gone from the desktop.

`assert_launch_gone`/`assert_bridge_gone` check the scope cgroup, the
container in the silo's store, the runsc state tree, the per-launch dir,
the control record, and both bridge pids — the complete residue list.

### S4 — the silo launches again (via the documented stop-then-start)

`state=Active` is intent, so a bare `StartSilo` after an app-exit is a
deliberate idempotent no-op (`session_manager.py` — "reports success
without launching anything"; relaunch is stop-then-start). Verify that
contract explicitly — the call succeeds but nothing launches — then take
the documented path: `StopSilo`, then `StartSilo` for real.

```bash
B64=$(base64 -w0 <<'EOF'
source /var/tmp/t3s-dl/tier3s-guest-lib.sh
SILO=t3scls; UNIT=$(unit_of "$SILO")
TOK1=$(sed -n 's/^TOK=//p' /tmp/s59-tok)
# Contract check: StartSilo while state=Active returns success yet must
# NOT start the unit (idempotent no-op, not a hidden relaunch).
sm StartSilo s "$SILO" >/dev/null \
    || { echo "FAIL: StartSilo on Active silo errored"; exit 1; }
sleep 5
[ "$(unit_state "$UNIT")" = inactive ] \
    || { echo "FAIL: bare StartSilo relaunched the unit — the no-op contract changed; update this scenario"; unit_log "$UNIT" "" | tail -10; exit 1; }
obs_stopped() { silo_observed "$1" | grep -q '^stopped'; }
obs_stopped "$SILO" \
    || { echo "FAIL: observed status drifted: $(silo_observed "$SILO")"; exit 1; }
# Documented relaunch: stop the stale intent, then start.
sm StopSilo si "$SILO" 10 >/dev/null
[ "$(silo_state "$SILO")" = Stopped ] || { echo "FAIL: not Stopped after StopSilo"; exit 1; }
TOK2=$(up_gui_silo "$SILO")
[ -n "$TOK2" ] || { echo "FAIL: relaunch did not come up"; exit 1; }
[ "$TOK2" != "$TOK1" ] || { echo "FAIL: relaunch reused token $TOK1"; exit 1; }
echo "TOK=$TOK2" > /tmp/s59-tok2
t3s_window_handle "$SILO" > /tmp/s59-handle2
echo "relaunch up: token=$TOK2 handle=$(cat /tmp/s59-handle2)"
finish
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
qdwin_screenshot "$ART/s4-relaunch.png"
```

**Assert**: a new token (≠ S1's) and a `handle=` line; the screenshot
shows the `[3s:t3scls]` window back on the desktop.

### S5 — final teardown

```bash
B64=$(base64 -w0 <<'EOF'
source /var/tmp/t3s-dl/tier3s-guest-lib.sh
SILO=t3scls; UNIT=$(unit_of "$SILO")
TOK=$(sed -n 's/^TOK=//p' /tmp/s59-tok2); H=$(cat /tmp/s59-handle2)
cur=$(journal_cursor)
sm StopSilo si "$SILO" 10 >/dev/null
[ "$(silo_state "$SILO")" = Stopped ] || { echo "FAIL: silo not Stopped"; exit 1; }
wait_for 90 unit_down "$UNIT" || { echo "FAIL: unit still up"; exit 1; }
wait_for 30 bash -c "journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service --no-pager -o cat --after-cursor='$cur' | grep -q 'toplevel_removed handle=$H'" \
    || { echo "FAIL: no toplevel_removed for relaunch handle $H"; exit 1; }
assert_launch_gone relaunch-teardown "$TOK" "$SILO"
assert_bridge_gone relaunch-teardown "$TOK"
sm DeleteSilo s "$SILO" >/dev/null
[ "$(silo_state "$SILO")" = absent ] || { echo "FAIL: silo not deleted"; exit 1; }
set_rules none
assert_all_clear end
echo "final teardown clean"
finish
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: exit 0 — `assert_all_clear` verifies no tier3s artifacts
remain anywhere (accounts are the silo's and leave with it; scopes,
records, bridge dirs, runsc state are all checked).

## Teardown

```bash
B64=$(base64 -w0 <<'EOF'
source /var/tmp/t3s-dl/tier3s-guest-lib.sh
s=t3scls
sm StopSilo si "$s" 10 >/dev/null 2>&1 || :
sm DeleteSilo s "$s" >/dev/null 2>&1 || :
set_rules none
finish
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Known caveats

- **App-exit unwind is the subject.** The toplevel must be removed
  because the sandboxed process died; if the launch unit or any residue
  survives the container's exit, that is a FAIL — report what stayed up
  (unit, scope, bridge) rather than calling it a pass.
- **`state` vs `observed_status` is the contract.** `state` is user
  intent and stays `Active` after the app dies — deliberately
  (`Silo.to_dict`, `observe_runtime_once`: observations are "ephemeral
  evidence, never lifecycle authority"). The runtime truth is
  `observed_status`, refreshed ~every 10 s; it must read `stopped` with
  `launcher inactive and workload boundary observed absent`. Asserting
  `state != Active` here would be wrong.
- **Relaunch after app-exit needs `StopSilo` first** — `start()` from
  `Active` is an idempotent no-op that returns success without launching
  (see the `StartNotCancelled` comment in `session_manager.py`). The
  scenario pins that no-op in S4 so a future semantic change (e.g.
  reconcile-on-start) fails loudly instead of passing silently.
- **Product finding surfaced by this scenario:** a user who closes a
  tier3s window and relaunches via `StartSilo` alone gets a silent
  no-op — no window, no error. Whether `StartSilo` should reconcile an
  `Active`-intent/`stopped`-observed silo is a product question; this
  test pins today's documented contract and the finding is tracked in
  `todo/open-followups.md`.
- **Silo accounts persist across launches** by design (the store and
  subuid rows are the silo's); `assert_all_clear` does not flag the
  account, only launch residue.
