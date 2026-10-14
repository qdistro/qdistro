# 01 — TUI approver in a real terminal: render, help overlay, scope picker, deny

<!-- qci:visual: required -->

**Lane: qdwin** (the product session, qdwin compositor + qdshell on
`wayland-1`). Read the "qdwin lane" and "The admin TUI in foot" sections of
`AGENTS.md` first.

**Terminal: foot, a TEST client.** The TUI under test is the INSTALLED
`/usr/local/bin/qdistro-admin-tui` (the image's own installer put it there),
started with no arguments as the admin uid. It runs inside `foot`, which every
GUI golden bakes as a core test client and which the shipped image does NOT
contain (its terminal is qterminator). This scenario tests the TUI; it gives no
qterminator coverage.

**What**: launch the installed TUI in foot, check its empty state, open and
dismiss the `?` help overlay, inject one pending request, check the populated
state and the scope picker (`2`, `3`, `1`), deny with `d`, and prove the deny
was the TUI's decision.

**Why**: Pilot unit tests (tests/unit/test_tui.py) cover the model and the
help text with fakes; only a real terminal shows colour-only cues (selection,
key chips, the bold scope label, the `⚠` warning), legibility and clipping of
the subtitle, footer and help modal, and real keyboard delivery. The deny is a
real authority call: the broker admits it only from the installed TUI in its
isolated shape, holding the `org.qdistro.broker.approve` grant (the CI
test-only polkit rule; this scenario does not test the password prompt).

Merged here, assertions intact: the former 02 (scope picker) and the visual
half of 05 (help overlay; its text content is also a unit test).

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot, qdwin_send_key, qdwin_chord (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}

# Session up, work/work2 silo fixtures (the requester is `work`), idle locker
# held off and proven unlocked. A nonzero exit is a Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup --silos'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_tui && qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
# No cached decision or rule may answer test.action for us: S4 needs a fresh
# pending row.
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

### S1 — launch the installed TUI in foot, empty state

```bash
# Bounded: foot's own window mapped, its child verified as the installed TUI
# in the broker's authority shape, and that TUI mounted (first GetPending on
# the bus). A nonzero exit FAILS S1 (an install/identity failure is ERROR).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_tui pg01'
sleep 2   # Textual paints after mount; the locked title cannot signal it
qdwin_screenshot "$ART/01-s1-empty.png"
```

**Assert:**
- The start helper printed `identity: OK` with
  `argv=['/usr/bin/python3', '-I', '/usr/local/bin/qdistro-admin-tui']`,
  `uid=1000` and a `TERM` set by foot: the TUI on screen is the installed one.
- A foot window is visible on the qdwin desktop (qdshell's window chrome
  around it is expected).
- Inside the terminal, the TUI header reads `qdistro admin approvals (TUI)`
  followed by a clock `HH:MM:SS`; the subtitle reads
  `(no pending requests) • scope: Just this once`, with no `⚠`.
- The left pane shows a table header with columns `uid`, `action`, `exe`
  and no data rows.
- The right pane shows the literal text `(no request selected)`.
- No modal or overlay covers the main view.
- The footer shows keybinding chips in this order:
  `a Approve`, `d Deny`, `^r Create Rule`, `shift+^a Approve All`,
  `shift+^d Deny All`, `1 1:once`, `2 2:1h`, `3 3:24h`, then `4` (further
  digit chips may follow as the width allows), with `^p palette` at the
  right end. OCR may render the modifier glyph as a curly quote or omit the
  caret; assert the labels and ordering rather than exact glyph shape.
  `a`, `d`, the modifier chips and the digits are highlighted (brighter than
  the labels).
- Nothing in the header, subtitle or footer is clipped. A clipped subtitle or
  footer is ERROR (terminal geometry), not FAIL; report the foot window size.

### S2 — `?` opens the help overlay

```bash
# Focus proven by the compositor on THIS foot before any key. A nonzero exit
# stops here: record ERROR, no key was sent.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_admin_tui' \
  || { echo "TUI-FOCUS-FAILED: record ERROR, no key was sent" >&2; exit 1; }
qdwin_chord shift -- slash      # `?` is Shift+/ on the VM's us layout
# MANDATORY SETTLE WAIT -- there is no guest-side signal for "the modal is on
# screen", and a capture ~0.1 s after the key still shows the main view
# (permissions-gui/05, 2026-09-25). Keep it.
sleep 2
qdwin_screenshot "$ART/01-s2-help.png"
```

If this frame still shows the main view, the key may not have been rendered
yet: wait 2 more seconds, capture ONCE more to a new name
(`qdwin_screenshot "$ART/01-s2-help-late.png"`) and judge S2 from that frame.
Record FAIL only if the late frame shows no modal either.

**Assert:**
- A bordered modal panel covers most of the content area (the main view's
  header/footer may show around it — Textual ModalScreen).
- The modal's first line is bold `qdistro admin TUI`.
- It contains the sections **Decide current:**, **Scope:** and
  **Navigation:**, in that order.
- Under "Scope:", all eight numbered lines are present, in order:
  `1 Just this once` (followed by `(default; no cache write)`), `2 1 hour`,
  `3 24 hours`, `4 Forever, any command from this user`,
  `5 Forever, only this exact program`, `6 Forever, only this exact argv tuple`,
  `7 Forever, this argv basename anywhere`,
  `8 Forever, this argv prefix + any trailing args` (alignment whitespace may
  differ).
- Under "Decide current:", both `a / Ctrl+Y` (Approve) and `d / Ctrl+N`
  (Deny) are listed.
- The last paragraph mentions "mirrored to the GUI app instantly".
- All of it is legible and unclipped (clipping from terminal geometry is
  ERROR, as in S1).

### S3 — Escape dismisses the overlay; the main view returns intact

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_admin_tui' \
  || { echo "TUI-FOCUS-FAILED: record ERROR, no key was sent" >&2; exit 1; }
qdwin_send_key KEY_ESC
# MANDATORY SETTLE WAIT: Textual also delays a bare Escape while it waits to
# see whether an escape sequence follows.
sleep 2
qdwin_screenshot "$ART/01-s3-dismissed.png"
```

Same single late re-capture rule as S2 (`01-s3-dismissed-late.png`).

**Assert:**
- The modal is gone; the main view is visible again with the same header,
  left pane and right pane as S1 — no residual overlay artefacts, no stray
  border fragments.
- The subtitle still reads `(no pending requests) • scope: Just this once`.

### S4 — inject one pending request, populated state (default scope)

```bash
# The guest clock is the bus trace's clock. Keep only the number (vm-exec may
# add its own diagnostic lines); an empty value stops the step.
since=$($VMEXEC "$VM" 'date +%s.%N' | grep -E '^[0-9]+[.][0-9]+$' | tail -n1)
[ -n "$since" ] || { echo "no guest timestamp: record ERROR" >&2; exit 1; }
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start 01-work work 'python3 /usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# The request is in the broker AND the TUI fetched its queue after the
# broker announced it. A timeout FAILS S4.
$VMEXEC "$VM" "source /tmp/qci-gui-waiters.sh && await_broker_pending_action test.action 30 && qdwin_admin_tui_await_refresh $since 40"
sleep 1   # repaint after the refresh
qdwin_screenshot "$ART/01-s4-populated.png"
```

**Assert:**
- The subtitle reads `1 pending • scope: Just this once` (a non-zero count
  instead of `(no pending requests)`), with **no** `⚠` glyph — the default
  scope is not warned about.
- The left pane has exactly one data row whose `uid` cell reads `2000` (or
  `work`) and whose `action` cell reads `test.action`; the row is
  highlighted as the current selection (inverse video or coloured
  background distinguishing it from the header, and the `▶` cursor glyph).
- The right pane shows the request detail — at least the action name
  `test.action` and the requesting uid — and the literal
  `(no request selected)` is gone. It shows `Scope: Just this once`, with
  "Just this once" rendered bold after the `Scope:` prefix.
- The footer chips are unchanged from S1. They are binding metadata and do
  not change with the selected scope: do not assert on their colour.

### S5 — press `2` (1 hour)

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_admin_tui' \
  || { echo "TUI-FOCUS-FAILED: record ERROR, no key was sent" >&2; exit 1; }
qdwin_send_key KEY_2
sleep 1
qdwin_screenshot "$ART/01-s5-1h.png"
```

**Assert:**
- Right pane: `Scope: 1 hour` (bold "1 hour").
- Subtitle scope chunk: `⚠ scope: 1 hour` — any non-`once` scope shows the
  warning.

### S6 — press `3` (24 hours)

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_admin_tui' \
  || { echo "TUI-FOCUS-FAILED: record ERROR, no key was sent" >&2; exit 1; }
qdwin_send_key KEY_3
sleep 1
qdwin_screenshot "$ART/01-s6-24h.png"
```

**Assert:**
- Right pane: `Scope: 24 hours` (bold "24 hours").
- Subtitle: `⚠ scope: 24 hours`.

### S7 — press `1` (back to once)

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_admin_tui' \
  || { echo "TUI-FOCUS-FAILED: record ERROR, no key was sent" >&2; exit 1; }
qdwin_send_key KEY_1
sleep 1
qdwin_screenshot "$ART/01-s7-once.png"
```

**Assert:**
- Right pane: `Scope: Just this once`.
- Subtitle: `scope: Just this once` with the `⚠` gone — pressing `1`
  returns to the default, non-warned state.

### S8 — deny with `d`; empty state returns; the TUI's deny reached the requester

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_admin_tui' \
  || { echo "TUI-FOCUS-FAILED: record ERROR, no key was sent" >&2; exit 1; }
qdwin_send_key KEY_D
# The requester completes (bounded; a TIMEOUT is this step's failure).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 01-work 60'
sleep 1
qdwin_screenshot "$ART/01-s8-afterdeny.png"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 01-work; echo "rc=$(bg_rc 01-work)"'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; echo "pending_count=$(broker_pending_count)"'
# DENIED alone also means a timeout: tie it to the TUI's own decision call,
# the broker's RequestDecided and the audit row for that request.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_tui_decision_evidence test.action deny once'
```

**Assert:**
- The left pane is empty again; the right pane returns to
  `(no request selected)`.
- The subtitle returns to `(no pending requests) • scope: Just this once`.
- No error toast, dialog or red banner is visible.
- `bg_log 01-work` is exactly `DENIED` and `rc=1` (the requester completed
  and saw the deny).
- `pending_count=0`.
- The evidence command prints `OK: the TUI decided request <N> (deny, once)`:
  a `DecideRequest(<N>, deny, once)` from the TUI's own bus connection, the
  broker's `RequestDecided(<N>, deny)`, and the audit row
  `0|once|prompt|1000|2000` for that request. Anything else is FAIL: the
  `DENIED` came from somewhere other than this keypress.

## Teardown

```bash
# Keep the evidence logs as artifacts before the helper stops the trace.
$VMEXEC "$VM" 'cat /run/qci-bus-trace/admin-tui.log' > "$ART/01-bus-trace.log" 2>&1 || true
$VMEXEC "$VM" 'cat /run/qci-admin-tui/foot.log' > "$ART/01-foot.log" 2>&1 || true
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_tui'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
```

## Notes for the runner

- Keys go through the QMP helpers (`qdwin_send_key`, `qdwin_chord`) only;
  do not mix in `virsh send-key` (qdwin-helpers.sh: mixing the two paths can
  leave modifiers held).
- If the TUI fails to appear, the start helper prints foot's log; it is also
  at `/run/qci-admin-tui/foot.log` in the guest. An identity or install
  failure there (a wrapper instead of the installed module, missing Textual)
  is a golden-bake ERROR, not a product FAIL.
- The TUI uses Textual's default dark theme; exact RGB values are not stable
  across Textual upgrades. The assertions talk about *relative* contrast
  (brighter / inverse / highlighted), not hex codes.
- The help overlay also closes on `q`, Space, Enter and `?`; only Escape is
  asserted.
