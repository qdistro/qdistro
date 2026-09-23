# 11 — ext-workspace-v1 server: list + switch (v24 workspaces)

**What**: drive `qdwin-workspace-probe` against the headless compositor to
check qdwin's `ext_workspace_manager_v1` server (the v24 workspace
implementation, `qdwin/qdwin.c`): a freshly-bound manager sees the default
workspace set, and an `ext_workspace_handle_v1.activate` + `commit`
switches the compositor's active workspace, which the compositor reflects
back via the `state` ACTIVE bit and a `done`.

**Why**: workspaces are the v24 feature
(`todo/issues/qdshell/qdshell-workspaces-and-appearance-settings.md`,
`todo/decisions/qdwin-workspaces-ext-protocol.md`). The activate path is
the load-bearing one — it must hide the non-active workspaces' windows and
re-emit state so a taskbar (qdshell, or any ext-workspace client) tracks
the switch. This exercises the bind→replay→activate→state round-trip
headlessly, independent of qdshell.

**Non-visual**: asserts on the probe's exit code (it re-reads the active
index after activating and fails nonzero on a mismatch) and on the
compositor's `active_workspace=` log line. No screenshots — the
window-visibility side of a switch is covered by the VM GUI scenario
`tests/gui/18-workspace-switch.md`.

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-workspace-probe"
ID=11-workspace
# QDWIN_WORKSPACE_COUNT pins the default count so the assertions are
# deterministic regardless of the build-time default.
QDWIN_WORKSPACE_COUNT=4 $HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)
WPID=$(ht_pid_load $ID weston)
run() { XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" "$@"; }
```

> Note: `start.sh` does not forward `QDWIN_WORKSPACE_COUNT` into weston's
> environment by itself — if the harness scrubs env, drop the
> `--expect-count` assertion in S1 and assert only `workspaces >= 1`. The
> activate assertion (S2) is independent of the exact count.

## Steps

### S1 — a freshly bound manager lists the workspace set

```bash
if run --expect-count=4; then RC=0; else RC=$?; fi
if [ "$RC" -eq 0 ]; then echo "S1 PASS"; else echo "S1 FAIL (exit=$RC)"; fi
```

**Assert (S1):**
- `RC == 0` — the manager advertised exactly 4 workspaces, one flagged
  active, terminated by a `done`. The probe exits nonzero on a count
  mismatch or protocol error. HARD.

### S2 — activate switches the compositor's active workspace

```bash
PRE=$(grep -cF "active_workspace=2/4" "$WLOG" || true)
if run --activate=2; then RC=0; else RC=$?; fi
POST=$(grep -cF "active_workspace=2/4" "$WLOG" || true)
if [ "$RC" -eq 0 ] && [ "$POST" -gt "$PRE" ]; then echo "S2 PASS"; else
    echo "S2 FAIL (exit=$RC want 0; active-log $PRE->$POST want grow)"; fi
```

**Assert (S2):**
- `RC == 0` — after `activate(2) + commit` the probe re-read the active
  index and it was 2 (the compositor honoured the switch and re-broadcast
  the ACTIVE state bit). HARD.
- the weston log gained an `active_workspace=2/4` line — proves the
  switch reached `qdwin_set_active_workspace`, not just a client-side echo.
  HARD.

### S3 — compositor stays alive after the switch

```bash
if kill -0 "$WPID" 2>/dev/null; then echo "S3 PASS"; else echo "S3 FAIL (weston died)"; fi
```

**Assert (S3):** `kill -0 weston` succeeds — the activate/hide path did not
crash the compositor. HARD.

## Teardown

```bash
$HT/stop.sh $ID
```

## Pass criteria

S1 (`RC==0`, 4 workspaces), S2 (`RC==0` + `active_workspace=2/4` log), and
S3 (weston alive) all hold.

## Known-broken-if

- S1 `workspaces=0` → the manager bound but sent no workspace handles.
  Check `qdwin_ext_ws_manager_create_handles` runs `workspace_count`
  iterations and that `bind_ext_workspace_manager` sends `done`.
- S2 probe reports `active=0` after activate → the activate request didn't
  reach `qdwin_set_active_workspace`, or the post-switch state wasn't
  re-broadcast. Check `qdwin_ext_ws_handle_activate` →
  `qdwin_ext_ws_broadcast_state`.
- S3 weston dies → likely a NULL deref in the workspace layer move or
  refocus path. Check `qdwin_toplevel_apply_workspace_visibility` /
  `qdwin_workspace_refocus_seats` guards.
```
