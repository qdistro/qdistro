# 40 — TUI survives a broker restart, shows the broker offline, recovers

<!-- qci:visual: required -->

**Lane: qdwin** (the product session, qdwin compositor + qdshell on
`wayland-1`). Read the "qdwin lane" and "The admin TUI in foot" sections of
`AGENTS.md` first.

**Terminal: foot, a TEST client.** The TUI under test is the INSTALLED
`/usr/local/bin/qdistro-admin-tui`, started with no arguments as the admin
uid, inside `foot` — a core test client of every GUI golden that the shipped
image does NOT contain (its terminal is qterminator). This scenario tests the
TUI; it gives no qterminator coverage.

**What**: one long-running TUI process lives through (a) a broker restart,
after which a new request must reach it by the broker's `RequestPending`
SIGNAL (not its 30 s safety poll) and a Ctrl+N deny must reach the
requester; then (b) a broker stop, which must turn its subtitle into a sticky
`⚠ BROKER OFFLINE — press r to retry` banner and empty its queue; and (c) a
broker start plus `r`, which must restore the normal subtitle.

**Why**: dbus-python's `add_signal_receiver(... bus_name=...)` resolves the
well-known name to a unique sender once; after a broker restart such a filter
silently stops delivering. The TUI's `DBusBrokerClient` subscribes without a
`bus_name`, so new-owner signals keep arriving — this is the acceptance test
of that, for the TUI (08 is the Qt app's). A transient toast fades and would
leave a deceptively healthy chrome while the broker is down; the sticky
subtitle override is the affordance (b) and (c) accept. The same TUI process
is checked (pid + start time) at every phase: a relaunch must not satisfy
any of them.

Merged here, assertions intact: the former 09 (broker-offline banner and
recovery), which ran on its own TUI process.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot, qdwin_send_key, qdwin_chord (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}
# The guest clock is the bus trace's clock; keep only the number.
guest_now() { $VMEXEC "$VM" 'date +%s.%N' | grep -E '^[0-9]+[.][0-9]+$' | tail -n1; }

$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup --silos'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_tui && qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
# No cached approval or rule may answer test.action (S3 needs a fresh pending
# row). Pending requests live in the broker's memory: the restart drains them,
# and the count proves it.
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh || exit 2
rm -f /etc/qdistro/rules.d/*test.action* 2>/dev/null
qci_sqlite approvals "DELETE FROM approvals WHERE action='test.action';"
systemctl restart qdistro-admin-broker.service
await_system_unit_active qdistro-admin-broker.service || exit 1
n=$(broker_pending_count) || exit 1
echo "pending_count=$n"
[ "$n" = 0 ]
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Steps

### S1 — launch the installed TUI on a clean broker, healthy baseline

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_tui pg40'
sleep 2   # Textual paints after mount; the locked title cannot signal it
qdwin_screenshot "$ART/40-s1-healthy.png"
```

**Assert:**
- The start helper printed `identity: OK` with
  `argv=['/usr/bin/python3', '-I', '/usr/local/bin/qdistro-admin-tui']`.
- The header subtitle reads `(no pending requests) • scope: Just this once`,
  with no `⚠` glyph and no `BROKER OFFLINE` banner.
- The right pane shows `(no request selected)`.

### S2 — restart the broker while the TUI stays up

```bash
$VMEXEC "$VM" 'systemctl show -p MainPID --value qdistro-admin-broker.service' > "$ART/40-broker-pid-before.txt"
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl show -p MainPID --value qdistro-admin-broker.service' > "$ART/40-broker-pid-after.txt"
echo "before=$(grep -E '^[0-9]+$' "$ART/40-broker-pid-before.txt") after=$(grep -E '^[0-9]+$' "$ART/40-broker-pid-after.txt")"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_tui_alive'
```

**Assert:**
- Broker MainPID before and after are both nonzero and differ (the service
  really restarted).
- `qdwin_admin_tui_alive` printed `same processes: ...` and `identity: OK`:
  the TUI started in S1 is still running, unrelaunched.

### S3 — a new request reaches the TUI by the broker's signal

This is the crux: a well-known-name-filtered subscription would silently drop
the new broker's `RequestPending`, and the TUI would stay empty (or catch up
only on its 30 s safety poll) while the broker holds the request.

```bash
# Inject at a moment clear of the safety poll's ticks, so a signal-driven
# refresh can never be mistaken for (or masked by) a poll.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_tui_clear_of_poll'
since=$(guest_now); [ -n "$since" ] || { echo "no guest timestamp: record ERROR" >&2; exit 1; }
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start 40-work work 'python3 /usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_broker_pending_action test.action 30'
s3_rc=0
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh && qdwin_admin_tui_await_signal_refresh $since 20" || s3_rc=$?
echo "s3 signal-refresh rc=$s3_rc"
sleep 1   # repaint after the refresh
qdwin_screenshot "$ART/40-s3-pending.png"
```

**Assert:**
- `s3 signal-refresh rc=0`, and the waiter's evidence line reads
  `SIGNAL-DRIVEN: RequestPending(<N>) at ..., TUI GetPending at ... (+<1-2>s)`.
  A timeout whose last observation says `no TUI GetPending within 2s after it`
  is a FAIL (the subscription did not deliver). One that says `INCONCLUSIVE`
  (the refresh coincided with a poll tick despite the guard) is ERROR.
- The frame shows one pending row with uid `2000` and action `test.action`
  in the left-side table.
- The right pane shows the request details: `uid=2000 pid=<N>`,
  `Action: test.action`, the executable path, and the details.

### S4 — deny with Ctrl+N; empty again; the SDK saw DENIED

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_admin_tui' \
  || { echo "TUI-FOCUS-FAILED: record ERROR, no key was sent" >&2; exit 1; }
qdwin_chord ctrl -- n
# bg_wait, never `wait $(cat X.pid)`. A TIMEOUT here IS this step's failure.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 40-work 60'
sleep 1  # Textual repaint after the deny; foot's locked title cannot signal TUI state
qdwin_screenshot "$ART/40-s4-afterdeny.png"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; if ! out=$(bg_log 40-work); then echo WORK_SAW_DENIED=unreadable; elif printf "%s\n" "$out" | grep -x DENIED >/dev/null; then echo WORK_SAW_DENIED=yes; else echo WORK_SAW_DENIED=no; fi'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_tui_decision_evidence test.action deny once && qdwin_admin_tui_alive'
```

**Assert:**
- The frame shows an empty table and the right pane `(no request selected)`.
- `WORK_SAW_DENIED=yes` — the SDK actually saw the deny, not just the UI
  (`no` is a product FAIL; `unreadable` is a harness ERROR).
- The evidence command prints `OK: the TUI decided request <N> (deny, once)`
  and the same-process check passes: the deny was the TUI's own decision
  call, on the restarted broker, from the process started in S1.

### S5 — stop the broker: sticky offline banner, queue cleared

Strengthened over the former 09: a row is visible BEFORE the broker stops, so
the empty table afterwards demonstrates that the TUI clears stale rows on a
broker error (rather than merely starting empty).

```bash
since=$(guest_now); [ -n "$since" ] || { echo "no guest timestamp: record ERROR" >&2; exit 1; }
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start 40-stale work 'python3 /usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh && await_broker_pending_action test.action 30 && qdwin_admin_tui_await_refresh $since 40"
sleep 1
qdwin_screenshot "$ART/40-s5-before-stop.png"
# Stop (not restart): the broker stays down for a measurable interval.
$VMEXEC "$VM" 'systemctl stop qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl is-active qdistro-admin-broker.service; true'
# The first safety poll after the stop is what turns the state sticky
# (POLL_INTERVAL_S = 30 s): wait well past it.
sleep 35
qdwin_screenshot "$ART/40-s5-offline.png"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_tui_alive'
```

**Assert:**
- `40-s5-before-stop.png` shows one `test.action` row for uid `2000`.
- `systemctl is-active` printed `inactive` (or `failed`): the broker really
  is down.
- In `40-s5-offline.png` the subtitle is replaced by
  `⚠ BROKER OFFLINE — press r to retry (<error detail>)`. The detail after
  the parenthesis varies (a bounded label such as a connection or
  service-unknown error); `BROKER OFFLINE` and `press r to retry` must be
  present.
- The left table is empty — the row from `40-s5-before-stop.png` is gone
  (the TUI clears its queue on a broker error so a later decide cannot hit a
  stale row) — and the right pane shows `(no request selected)`.
- The same TUI process is still running (same-process check passed).

### S6 — start the broker, press `r`: recovered

```bash
$VMEXEC "$VM" 'systemctl start qdistro-admin-broker.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_admin_tui' \
  || { echo "TUI-FOCUS-FAILED: record ERROR, no key was sent" >&2; exit 1; }
qdwin_send_key KEY_R
sleep 1
qdwin_screenshot "$ART/40-s6-recovered.png"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_tui_alive'
```

**Assert:**
- The subtitle is back to `(no pending requests) • scope: Just this once`.
- No `BROKER OFFLINE` text and no `⚠` glyph.
- A green info toast `broker connection restored` may be visible in the
  lower right (shown for ~4 s). Presence is a bonus; absence is fine if the
  frame landed after it faded.
- The same TUI process is still running.

## Teardown

```bash
# The broker must be running again whatever happened above.
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'cat /run/qci-bus-trace/admin-tui.log' > "$ART/40-bus-trace.log" 2>&1 || true
$VMEXEC "$VM" 'cat /run/qci-admin-tui/foot.log' > "$ART/40-foot.log" 2>&1 || true
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_tui'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
```

## Notes for the runner

- Do NOT kill or relaunch the TUI between S1 and S6; the whole point is the
  _long-running_ TUI. The same-process checks enforce it.
- Keys go through the QMP helpers (`qdwin_send_key`, `qdwin_chord`) only;
  do not mix in `virsh send-key`.
- If S3 shows an empty table, check the same-process line and
  `/run/qci-admin-tui/foot.log` first: a crashed TUI is a different bug from
  the signal-subscription regression S3 targets.
- S3 asserts that the new request arrived by signal. It does not establish
  which internal path (`_reconnect()` or a fresh owner-bound proxy) served
  the TUI's next broker call; the acceptance is that the TUI kept working.
- The 35 s wait in S5 is deliberate (POLL_INTERVAL_S is 30 s). Shorter waits
  may catch the TUI mid-error-handling.
- `40-stale`'s requester loses its broker in S5 and exits on its own; its
  result is not asserted.
