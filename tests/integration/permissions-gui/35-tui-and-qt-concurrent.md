# 35 — TUI and Qt admin app stay in sync via broker signals

<!-- qci:visual: required -->

**Lane: qdwin** (the product session, qdwin compositor + qdshell on
`wayland-1`). Read the "qdwin lane" and "The admin TUI in foot" sections of
`AGENTS.md` first.

**Programs under test**: the shipped Qt admin app, started through the shipped
native-Wayland launcher, and the INSTALLED `/usr/local/bin/qdistro-admin-tui`,
started with no arguments as the admin uid inside `foot` — a core test client
of every GUI golden that the shipped image does NOT contain (its terminal is
qterminator; this scenario gives no qterminator coverage).

**What**: run the Qt admin app AND the Textual TUI at the same time (both
subscribe to the broker's `RequestPending` / `RequestDecided`). Inject one
pending request as `work`; both show it. Approve in the Qt app (Ctrl+Y); the
TUI's pending list must empty BECAUSE of the broker's `RequestDecided`
signal — not its 30 s safety poll — and the TUI must not try to decide the
request itself.

**Why**: `permissions.md` pins no "one approver per session" rule, and the
broker's signal design supports concurrent subscribers (the admin's desktop
plus a future mobile approver). A TUI that kept its own stale view would let
two approvers double-decide a request; the second decide would return
`already-allow`/`unknown`, and the admin's mental model would be muddy.

**Layout change from the labwc-era scenario**: the two windows are no longer
asserted in ONE frame. foot runs maximized, so each step captures one frame
per UI (focusing a window raises it; focus alone sends no key). Both
processes stay alive throughout, and the per-step frames plus the bus trace
are what show two concurrent subscribers.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot, qdwin_chord (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}
guest_now() { $VMEXEC "$VM" 'date +%s.%N' | grep -E '^[0-9]+[.][0-9]+$' | tail -n1; }

$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup --silos'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_tui && qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
# A persistent test.action approval or rule would answer the request without a
# prompt; restarting the broker does NOT clear the approvals cache, so clear it
# explicitly. The restart drains the in-memory pending queue; prove it.
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

### S1 — launch the TUI, then the Qt admin app; both empty

```bash
# TUI first (its start also starts the broker bus trace `admin-tui`, which
# records the Qt app's calls too), then the shipped Qt app in first-paint mode.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_tui pg35'
qt_pid=$($VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_app' | grep -E '^[0-9]+$' | tail -n1)
[ -n "$qt_pid" ] || { echo "admin app did not start: S1 FAILS" >&2; exit 1; }
echo "qt_pid=$qt_pid"
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh && await_qdwin_window_title 'admin approvals' 30 1 $qt_pid"
qdwin_screenshot "$ART/35-s1-qt-empty.png"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_admin_tui' \
  || { echo "TUI-FOCUS-FAILED: record ERROR" >&2; exit 1; }
sleep 1
qdwin_screenshot "$ART/35-s1-tui-empty.png"
```

**Assert:**
- The TUI start helper printed `identity: OK` with
  `argv=['/usr/bin/python3', '-I', '/usr/local/bin/qdistro-admin-tui']`, and
  the admin app started (`qt_pid=<N>`); both run concurrently from here on.
- `35-s1-qt-empty.png`: the Qt `admin approvals` window, pending list empty,
  detail pane `(no selection)`.
- `35-s1-tui-empty.png`: the TUI in foot, subtitle
  `(no pending requests) • scope: Just this once`, empty table, right pane
  `(no request selected)`.

### S2 — inject one pending request; both UIs show it

```bash
# Inject clear of the TUI's safety-poll ticks, so its refresh is provably the
# signal's.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_tui_clear_of_poll'
since=$(guest_now); [ -n "$since" ] || { echo "no guest timestamp: record ERROR" >&2; exit 1; }
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start 35-work work '/usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_broker_pending_action test.action 30'
s2_tui=0
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh && qdwin_admin_tui_await_signal_refresh $since 20" || s2_tui=$?
s2_qt=0
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh && await_qdwin_window_title 'admin approvals \(1 pending\)' 30 1 $qt_pid" || s2_qt=$?
echo "s2 tui-signal-refresh rc=$s2_tui qt-title rc=$s2_qt"
sleep 1
qdwin_screenshot "$ART/35-s2-tui-row.png"       # the TUI still has focus
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh && qdwin_focus_window 'admin approvals.*' 15 $qt_pid"
qdwin_screenshot "$ART/35-s2-qt-row.png"
```

**Assert:**
- `s2 tui-signal-refresh rc=0 qt-title rc=0`. The TUI waiter's evidence line
  reads `SIGNAL-DRIVEN: RequestPending(<N>) ...`: the broker emitted
  `RequestPending` and the TUI reacted to it, not to its poll. (A timeout
  saying `no TUI GetPending within 2s after it` is FAIL; one saying
  `INCONCLUSIVE` is ERROR.) The Qt app's title counts the row it displays;
  its own signal path after a broker restart is permissions-gui/08's subject.
- `35-s2-tui-row.png`: the TUI's pending list shows the request — action
  `test.action`, uid `2000`.
- `35-s2-qt-row.png`: the Qt pending list shows one row `uid=2000 test.action`.

### S3 — approve in the Qt app with Ctrl+Y; the TUI empties by the signal

Run this block in ONE go: the TUI frame and the signal check must land before
the TUI's next safety poll (the guard gives 20 s).

```bash
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh && qdwin_focus_window 'admin approvals.*' 15 $qt_pid" \
  || { echo "QT-FOCUS-FAILED: record ERROR, no key was sent" >&2; exit 1; }
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_tui_clear_of_poll 20'
since=$(guest_now); [ -n "$since" ] || { echo "no guest timestamp: record ERROR" >&2; exit 1; }
# Journal cursor BEFORE the keystroke (S4's supplementary diagnostic).
$VMEXEC "$VM" 'journalctl -u qdistro-admin-broker.service -n0 --show-cursor 2>/dev/null | sed -n "s/^-- cursor: //p" > /tmp/35-decide.cur'
qdwin_chord ctrl -- y
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 35-work 60'
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh && await_qdwin_window_title 'admin approvals' 30 1 $qt_pid"
qdwin_screenshot "$ART/35-s3-qt-emptied.png"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_admin_tui' \
  || { echo "TUI-FOCUS-FAILED: record ERROR" >&2; exit 1; }
sleep 0.5
qdwin_screenshot "$ART/35-s3-tui-emptied.png"
s3_rc=0
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh && qdwin_admin_tui_removed_by_signal $since" || s3_rc=$?
echo "s3 tui-removed-by-signal rc=$s3_rc"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 35-work; echo "rc=$(bg_rc 35-work)"'
```

**Assert:**
- `35-s3-qt-emptied.png`: the Qt pending list is empty; detail pane
  `(no selection)`.
- `35-s3-tui-emptied.png`: the TUI's pending list is also empty — it received
  the broker's `RequestDecided` and removed the row on its own. Read the
  TUI's pane content (`(no request selected)`, subtitle
  `(no pending requests) • ...`); an occluded window is not an empty list.
- `s3 tui-removed-by-signal rc=0`, printing `OK: RequestDecided(<N>,allow)
  ...; TUI made no DecideRequest; TUI GetPending since then: ...`: between the
  approval and this check the TUI's queue changed only through the signal
  (its cheap path makes no broker call; any refresh it made followed a
  signal). `rc=1` is FAIL; `rc=3` (`INCONCLUSIVE`, a safety poll ran in
  between) is ERROR.
- `bg_log 35-work` is `ALLOWED` and `rc=0`.

### S4 — invariant: exactly one decision, made by the Qt app; none by the TUI

```bash
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh && echo qt_decide_calls=\$(qci_bus_trace_count admin-tui call DecideRequest $qt_pid $since)"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && echo tui_decide_calls=$(qci_bus_trace_count admin-tui call DecideRequest "$(cat /run/qci-admin-tui/tui.pid)")'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_tui_alive >/dev/null && echo tui_same_process=yes'
# Supplementary diagnostic (not the oracle): broker log lines after the cursor.
$VMEXEC "$VM" 'cur=$(cat /tmp/35-decide.cur 2>/dev/null); [ -n "$cur" ] || { echo "no cursor"; exit 0; }; journalctl -u qdistro-admin-broker.service --after-cursor "$cur" --no-pager | grep -E "(invalid|gone|already|stale|unknown).*request" || echo "no double-decide lines after the approve cursor"'
```

**Assert:**
- `qt_decide_calls=1` — the trace saw the Qt app's one `DecideRequest` (the
  positive control: the trace was listening to decisions).
- `tui_decide_calls=0` — the TUI never called `DecideRequest` at all. The
  broker answers a second decide of a decided request with `already-allow`
  or `unknown` and writes no second audit row, so only the bus trace can see
  a double-decide attempt.
- `tui_same_process=yes`.
- The journal line is diagnostic only: a match there with the counts above
  clean is noted in the report, not graded.

## Teardown

```bash
$VMEXEC "$VM" 'cat /run/qci-bus-trace/admin-tui.log' > "$ART/35-bus-trace.log" 2>&1 || true
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_tui; qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/35-decide.cur'
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh || exit 2
qci_sqlite approvals "DELETE FROM approvals WHERE action='test.action';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Notes for the runner

- Both UIs run as the admin uid on separate system-bus connections; the
  broker's signals reach each connection, so two subscribers get two copies.
- If S3 shows the Qt app emptied but the TUI still has the row, the TUI's
  signal subscription is broken (the cousin of the Qt bug fixed in `d72a430`,
  see 08).
- Keys go through the QMP helper (`qdwin_chord`) only; do not mix in
  `virsh send-key`.
