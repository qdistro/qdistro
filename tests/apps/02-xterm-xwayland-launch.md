# 02 — xterm launches without crashing weston

<!-- qci:visual: required -->

**Acceptance criterion:** spawning an XWayland client (xterm) does not
SIGSEGV weston. Regression-tests qdwin bug #2 (NULL `wl_client` from
`weston_desktop_client_get_client` for XWayland-spawned surfaces was
dereferenced inside `qdwin_client_uid`).

## Setup

```bash
source ${QDWIN_REPO}/tests/apps/qdwin-apps-helpers.sh
qdwin_apps_set_vm "${VMNAME}"
qdwin_apps_session_up || { echo "FAIL: bystander/weston not healthy"; exit 1; }

# Snapshot the actual guest weston PID directly.  A qemu guest-exec response
# contains the transient command PID, not its captured stdout, and a historical
# journal grep can return stale/non-numeric text; neither is valid evidence.
WESTON_PID_BEFORE=$("$QDWIN_VM_EXEC" "$VMNAME" \
    'pgrep -u admin -x weston | head -1' 2>/dev/null)
case "$WESTON_PID_BEFORE" in
    ''|*[!0-9]*) echo "FAIL: expected numeric weston PID before xterm, observed '$WESTON_PID_BEFORE'" >&2; exit 1 ;;
esac
echo "weston pid before: $WESTON_PID_BEFORE"

# This scenario owns only xterm. Never stop Xwayland itself: doing so destroys
# the very compositor/session continuity that the weston-PID assertion tests.
# Check the setup cleanup immediately so it cannot invalidate the baseline
# before the actual launch regression begins.
qdwin_apps_kill xterm
WESTON_PID_AFTER_SETUP=$("$QDWIN_VM_EXEC" "$VMNAME" \
    'pgrep -u admin -x weston | head -1' 2>/dev/null)
[ "$WESTON_PID_BEFORE" = "$WESTON_PID_AFTER_SETUP" ] || {
    echo "FAIL: targeted setup cleanup restarted weston (pid $WESTON_PID_BEFORE → $WESTON_PID_AFTER_SETUP)" >&2
    exit 1
}
```

## Steps

### Step 1 — launch xterm

```bash
qdwin_apps_launch xterm "xterm -fa Monospace -fs 12"
sleep 6
qdwin_apps_screenshot /tmp/02-step1-xterm.png
```

**Assert (1.1):** screenshot shows xterm rendered with XWayland CSD
(grey title bar with min/max/close buttons, white content area, shell
prompt visible — `admin@…> ` or similar).
**Assert (1.2):** bystander log shows
`toplevel_added handle=<N> ... title="xterm" xwayland=1`. Note the
`xwayland=1` — that's the bug #3 fix for free.
**Assert (1.2b) — FINDING #6:** that same `toplevel_added` line shows
`owner_uid=4294967295` (i.e. `(uid_t)-1`, the explicit "unknown" sentinel),
NOT the compositor/admin uid. XWayland surfaces have `client==NULL` so
`qdwin_client_uid()` returns `(uid_t)-1`; downstream must treat unknown
identity as untrusted, never as admin-local. Assert with:

```bash
qdwin_apps_log_grep 'toplevel_added .*owner_uid=4294967295 .*title="xterm" xwayland=1' \
  || echo "FAIL: XWayland toplevel not attributed to uid=unknown (-1)"
ADMIN_UID=$("$QDWIN_VM_EXEC" "$VMNAME" 'id -u admin')
qdwin_apps_log_grep "toplevel_added .*owner_uid=${ADMIN_UID} .*title=\"xterm\"" \
  && echo "FAIL: XWayland toplevel attributed to ADMIN uid (FINDING #6 regressed)" || true
```
**Assert (1.3):** weston pid did NOT change between Setup and now —
proving qdwin didn't crash. Use:

```bash
WESTON_PID_AFTER=$("$QDWIN_VM_EXEC" "$VMNAME" 'pgrep -u admin -x weston | head -1')
[ "$WESTON_PID_BEFORE" = "$WESTON_PID_AFTER" ] || {
    echo "FAIL: weston restarted (pid $WESTON_PID_BEFORE → $WESTON_PID_AFTER)" >&2
    exit 1
}
```

### Step 2 — type into xterm

```bash
qdwin_apps_send_key KEY_E KEY_C KEY_H KEY_O KEY_SPACE KEY_Q KEY_D KEY_W KEY_I KEY_N KEY_ENTER
sleep 1
qdwin_apps_screenshot /tmp/02-step2-typed.png
```

**Assert (2.1):** screenshot shows the typed command on one line and
output on the next. Tests that wl_keyboard delivery to XWayland
works after the NULL-client fix (the SIGSEGV used to fire during
`toplevel_added`, before the keyboard path was ever exercised).

## Cleanup

```bash
qdwin_apps_ctl "close" || true
# The FIFO write only proves command delivery. Ensure this scenario's xterm is
# gone even if the compositor could not service the close request.
qdwin_apps_kill xterm
```

## Pass criteria

- xterm window visible in step 1 screenshot.
- weston PID unchanged (no SIGSEGV restart).
- bystander log records `xwayland=1` (proves bug #3 fix is also live).
- bystander log records `owner_uid=4294967295` for the xterm toplevel
  (FINDING #6: XWayland attributed to uid=unknown, not admin).

## Known failure modes

- **Pre-fix regression (bug #2)** — xterm shows "X connection to :0
  broken (explicit kill or server shutdown)" in `/tmp/xterm.log`,
  weston pid changed, qdwin.log shows no `toplevel_added`. The
  `qdwin_client_uid` NULL guard at `qdwin.c:679` regressed.
- **Xwayland not installed** — qdwin.log line "launching '/usr/bin/Xwayland'"
  immediately followed by "exited with status 1". Need
  `zypper -n install xwayland`. Also see
   item 1.
