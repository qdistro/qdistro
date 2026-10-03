# 03 — is_xwayland flag distinguishes Wayland vs X11 clients

<!-- qci:visual: required -->

**Acceptance criterion:** `qdwin_shell_v1.toplevel_added` carries
`is_xwayland=0` for native-Wayland clients and `is_xwayland=1` for
XWayland-spawned X11 clients. Regression-tests qdwin bug #3 (was
hardcoded to 0 in `qdwin_send_toplevel_added` at `qdwin.c:752`).

## Setup

Run this block and every later block in the **host driver shell**. The helper
functions perform their own guest calls; do not source the helper in the VM or
check for the FIFO before `qdwin_apps_session_up` has initialized the
bystander.

```bash
source "$QDISTRO_REPO/qdwin/tests/apps/qdwin-apps-helpers.sh"
qdwin_apps_set_vm "${VMNAME}"
qdwin_apps_session_up || { echo "FAIL: bystander/weston not healthy"; exit 1; }
qdwin_apps_kill_all
```

## Steps

### Step 1 — launch foot (native Wayland)

```bash
qdwin_apps_launch foot "foot"
sleep 4
qdwin_apps_screenshot "$QCI_GUI_ARTIFACT_DIR/03-step1-foot.png"
```

**Assert (1.1):** screenshot shows the foot terminal window.
**Assert (1.2):** bystander log shows a line of the form
`toplevel_added handle=<N> app_id="foot" title="" xwayland=0`. The
trailing `xwayland=0` is the fix.

### Step 2 — launch xterm (XWayland) without closing foot

```bash
qdwin_apps_launch xterm "xterm"
sleep 4
qdwin_apps_screenshot "$QCI_GUI_ARTIFACT_DIR/03-step2-both.png"
```

**Assert (2.1):** screenshot shows the xterm window (foot may be
behind, partially visible, or off-screen — the bystander stacking is
naive).
**Assert (2.2):** bystander log shows a NEW line of the form
`toplevel_added handle=<M> ... title="xterm" xwayland=1`. The
`xwayland=1` is the load-bearing assertion for this scenario.
**Assert (2.3):** the foot toplevel's `xwayland=0` line is unchanged
(grep the bystander log; should still match exactly once).

### Step 3 — verify via shell command channel

```bash
TAGGING_LOG=$(qdwin_apps_log_grep 'toplevel_added') || {
    echo "FAIL: could not read bystander toplevel log" >&2; exit 1;
}
printf '%s\n' "$TAGGING_LOG" | tee "$QCI_GUI_ARTIFACT_DIR/03-step3-tagging.txt"
printf '%s\n' "$TAGGING_LOG" \
    | grep -Eq 'app_id="foot".*xwayland=0' || {
        echo "FAIL: foot was not tagged as native Wayland (xwayland=0)" >&2
        exit 1
    }
printf '%s\n' "$TAGGING_LOG" \
    | grep -Eq 'title="xterm".*xwayland=1' || {
        echo "FAIL: xterm was not tagged as XWayland (xwayland=1)" >&2
        exit 1
    }
```

**Assert (3.1):** `$QCI_GUI_ARTIFACT_DIR/03-step3-tagging.txt` contains both an
`xwayland=0` line for foot and an `xwayland=1` line for xterm. If
either line is missing OR if both apps got the same flag, the bug is
back.

## Cleanup

```bash
qdwin_apps_kill_all
```

## Pass criteria

- foot tagged `xwayland=0`.
- xterm tagged `xwayland=1`.
- Both lines present in bystander log.

## Known failure modes

- **Pre-fix regression (bug #3)** — both lines show `xwayland=0`. The
  `weston_xwayland_surface_get_api` lookup at `qdwin.c:752` regressed
  or the API isn't available in the loaded `xwayland.so`.
- **xwayland.so not loaded** — both apps tagged `xwayland=0` even
  after the fix because qdwin's API resolver returns NULL. Confirm
  with `qdwin_apps_log_grep 'Loading module.*xwayland.so'` showing a
  hit; if absent, weston was started without `--xwayland` and xterm
  itself shouldn't even run.
