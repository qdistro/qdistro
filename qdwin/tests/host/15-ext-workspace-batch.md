# 15 — ext-workspace-v1 server: bind-time output_enter + atomic commit

**What**: drive `qdwin-workspace-probe` against the headless compositor to
check two recently-added protocol behaviours of qdwin's
`ext_workspace_manager_v1` server (`qdwin/qdwin.c`):

1. **bind-time group→output association** — a freshly-bound manager sends the
   `workspace_group` and then, before the first manager `done`, an
   `output_enter` on that group for each `wl_output` the client has bound.
   This is what lets a per-monitor bar know the group spans its output.
2. **atomic commit staging** — `create_workspace` and `activate` are *staged*
   on the manager and only take effect on `commit`, as a batch. Issuing them
   without a `commit` must produce NO observable state change; the following
   `commit` must apply BOTH together.

**Why**: the ext-workspace spec requires `activate` / `create_workspace` /
`remove` to be applied atomically at the `commit` boundary, not eagerly per
request, and requires the group to be associated with its outputs via
`output_enter`. qdwin previously applied the requests eagerly and never sent
the group's `output_enter`; both were fixed in `qdwin/qdwin.c`
(`qdwin_ext_ws_manager_commit` now flushes a staged op list, and
`bind_ext_workspace_manager` calls `qdwin_ext_ws_group_send_output_enters`).
The list+switch happy path is covered by `11-workspace-protocol.md`; this
scenario covers the two-phase commit and the bind-time association
specifically.

**Non-visual**: asserts on the probe's exit code (it observes the
pre-commit/post-commit state itself and exits nonzero on a mismatch) and on
the compositor's `workspace created, count=` / `active_workspace=2/5` log
lines, which prove the staged ops actually reached
`qdwin_workspace_create` / `qdwin_set_active_workspace` on `commit` (not just
a client-side echo). No screenshots.

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-workspace-probe"
ID=15-ext-workspace-batch
# Default workspace count is 4, active 0 — both assertions key off the
# DELTA (count grows by one, active becomes 2), so they hold regardless of
# whether the harness forwards QDWIN_WORKSPACE_COUNT into weston's env.
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)
WPID=$(ht_pid_load $ID weston)
run() { XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" "$@"; }
```

> The headless backend advertises a single output, so S1 asserts
> `--expect-output-enter=1` (exact). On a multi-output backend the count
> differs; use the bare `--expect-output-enter` flag (asserts `>= 1`).
> The probe binds the `wl_output` global(s) BEFORE binding the manager —
> `output_enter` references a `wl_output` the client must already own, so a
> client that never binds `wl_output` correctly receives no `output_enter`.

## Steps

### S1 — the bind burst carries the group's output_enter

```bash
if run --expect-output-enter=1; then RC=0; else RC=$?; fi
if [ "$RC" -eq 0 ]; then echo "S1 PASS"; else echo "S1 FAIL (exit=$RC)"; fi
```

**Assert (S1):** `RC == 0` — within the bind burst (before the first manager
`done`) the group emitted exactly one `output_enter`, for the single output
the probe bound. The probe exits nonzero if the count was 0 (the group never
associated with the output) or a protocol error was posted. HARD.

### S2 — create_workspace + activate are staged and applied atomically on commit

```bash
PRE_C=$(grep -cF "workspace created" "$WLOG" || true)
PRE_A=$(grep -cF "active_workspace=2/5" "$WLOG" || true)
if run --batch-activate=2; then RC=0; else RC=$?; fi
POST_C=$(grep -cF "workspace created" "$WLOG" || true)
POST_A=$(grep -cF "active_workspace=2/5" "$WLOG" || true)
if [ "$RC" -eq 0 ] && [ "$POST_C" -gt "$PRE_C" ] && [ "$POST_A" -gt "$PRE_A" ]
then echo "S2 PASS"; else
    echo "S2 FAIL (exit=$RC want 0; created $PRE_C->$POST_C want grow; \
active2/5 $PRE_A->$POST_A want grow)"; fi
```

**Assert (S2):**
- `RC == 0` — the probe staged `create_workspace` + `activate(2)` WITHOUT a
  commit and observed NO state change (workspace count and active index
  unchanged); then on `commit` BOTH landed atomically (count grew by one and
  active became 2). The probe exits nonzero if any state changed before the
  commit (eager application) or if either op failed to apply on commit. HARD.
- the weston log gained a `workspace created, count=5` line and an
  `active_workspace=2/5` line — proves the staged ops reached
  `qdwin_workspace_create` / `qdwin_set_active_workspace` only at the commit
  boundary, not a client-side echo. HARD.

> S2 runs after S1 on the same compositor; S1 changes no state, so the active
> workspace is still 0 and `--batch-activate=2` names a non-active workspace
> (the probe refuses a batch index that is already active, so this can't
> false-pass).

### S3 — compositor stays alive after the batch commit

```bash
if kill -0 "$WPID" 2>/dev/null; then echo "S3 PASS"; else echo "S3 FAIL (weston died)"; fi
```

**Assert (S3):** `kill -0 weston` succeeds — staging + the batched create +
activate (which moves layers and refocuses) did not crash the compositor.
HARD.

## Teardown

```bash
$HT/stop.sh $ID
```

## Pass criteria

S1 (`RC==0`, one bind-time `output_enter`), S2 (`RC==0` + `workspace created`
and `active_workspace=2/5` log lines appear only after commit), and S3
(weston alive) all hold.

## Known-broken-if

- S1 `output_enter@bind=0` → the group never sent `output_enter` for the
  client's output at bind. Check `bind_ext_workspace_manager` calls
  `qdwin_ext_ws_group_send_output_enters`, and that
  `qdwin_output_resource_for_client` finds the client's `wl_output` resource
  (the probe binds `wl_output` before the manager).
- S2 `state changed BEFORE commit` → `activate` / `create_workspace` are
  still applied eagerly. Check `qdwin_ext_ws_handle_activate` /
  `qdwin_ext_ws_group_create_workspace` only call `qdwin_ext_ws_pending_add`,
  and that `qdwin_ext_ws_manager_commit` runs `qdwin_ext_ws_manager_flush`.
- S2 `commit did not apply ...` → the flush dropped or misordered the staged
  ops. Check `qdwin_ext_ws_manager_flush` replays the `pending` list
  front-to-back and that `qdwin_ext_ws_pending_add` appends (inserts at
  `pending.prev`).
- S3 weston dies → a NULL deref in the batched workspace create/activate
  path. Check the workspace layer-move / refocus guards.
```
