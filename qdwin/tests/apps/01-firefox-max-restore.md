# 01 — Firefox round-trips request_maximize → request_maximize(0)

<!-- qci:visual: required -->

**Acceptance criterion:** after a `max` then `restore` via the bystander
FIFO, Firefox is back at (approximately) its pre-max geometry — not
sticking at the maximised dimensions. Regression-tests qdwin bug #1
(saved `outer_*` was 0 before first `apply_inset`).

## Setup

```bash
source ${QDWIN_REPO}/tests/apps/qdwin-apps-helpers.sh
qdwin_apps_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdwin_apps_session_up || { echo "FAIL: bystander/weston not healthy"; exit 1; }
qdwin_apps_kill_all
```

## Steps

### Step 1 — launch firefox

```bash
qdwin_apps_launch firefox "firefox --no-remote --new-instance about:blank"
sleep 12
# Current Firefox releases can choose a maximised initial state even with a
# clean profile. Normalise that dependency-controlled state before recording
# the floating baseline; this keeps the compositor round-trip assertion
# deterministic without relaxing its size or edge checks.
qdwin_apps_ctl "restorelast"
sleep 2
qdwin_apps_screenshot /tmp/01-step1-launched.png
```

**Assert (1.1):** screenshot shows the Firefox window with tabs and
URL bar visible. Window does NOT fill the screen — there should be
black margin on at least one edge.
**Assert (1.2):** the bystander protocol log contains Firefox's effective
app ID, either in the initial `toplevel_added handle=<N> app_id="firefox"`
or in the v31 live update `toplevel_app_id handle=<N> app_id="firefox"`.
XWayland may publish WM_CLASS only after its first mapped commit, so an empty
app ID in `toplevel_added` is valid only when the non-empty v31 update follows.
(Verify via
`qdwin_apps_log_grep 'toplevel_(added|app_id).*app_id="firefox"'`.)

### Step 2 — maximise via shell

```bash
qdwin_apps_ctl "maxlast"
sleep 2
qdwin_apps_screenshot /tmp/01-step2-max.png
```

**Assert (2.1):** screenshot shows Firefox filling the entire 1280×800
output. No black margin on any edge. Tabs/URL bar at the very top.
**Assert (2.2):** bystander log shows `cmd max handle=<N>` followed by
`toplevel_state handle=<N> state=0x1` (bit 0 = maximised).

### Step 3 — restore via shell

```bash
qdwin_apps_ctl "restorelast"
sleep 2
qdwin_apps_screenshot /tmp/01-step3-restored.png
```

**Assert (3.1):** screenshot shows Firefox back at approximately its
launch size, NOT at the maximised dimensions. Black margin on some
edge again. Allow ±20 px wobble; absolute pixel match isn't required.
**Assert (3.2):** bystander log shows `cmd restore handle=<N>` then
`toplevel_state handle=<N> state=0x0`.
**Assert (3.3):** the restored window must NOT extend to the right or
bottom edge of the 1280×800 screen — that's the visual signature of
the pre-fix bug (saved geometry was 0 → restore picked up the
just-committed maximised size).

## Cleanup

```bash
qdwin_apps_ctl "close" || qdwin_apps_kill_all
```

## Pass criteria

- All three screenshots match assertions.
- The width/height of the window in step 3 must be within 20 px of step 1
  (i.e. restore worked).

## Known failure modes

- **Pre-fix regression (bug #1)** — step 3 shows Firefox at 1150×780
  (or near-maximised dimensions) at saved x/y. Means the
  `qdwin_handle_request_maximize` fix at `qdwin.c:1534` regressed.
- **No floating baseline after normalisation** — if `restorelast` still leaves
  step 1 maximised, the test is meaningless and must fail before step 2. Check
  the bystander state events and Firefox's profile/session restore policy.
