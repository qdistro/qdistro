# 08 — wp_security_context_v1 identity / commit-sequence edges

**What**: drive `qdwin-secctx-probe` against the headless compositor to
pin qdwin's `wp_security_context_v1` commit-sequence behavior
(`qdwin_secctx_set_sandbox_engine` / `set_app_id` / `set_instance_id` /
`qdwin_secctx_commit`, `qdwin/qdwin.c`): a commit with a missing app_id (or
no metadata at all) is a valid sequence and is ACCEPTED, while a
single-use context that is re-committed (or has a setter called after
commit) is REFUSED with the `already_used` protocol error.

**Why**: secctx tags are advisory routing metadata qdwin forwards to the
shell (`doc/protocol.md` "Security posture"). A client may legitimately
commit with partial/empty metadata, and qdwin must accept that (the
silo-resolution consequence — qdshell treating an empty app_id as
"unknown" — is a qdshell-side concern, not qdwin's). Conversely the
protocol marks a context single-use; qdwin must enforce that so a client
can't mutate or re-arm an already-committed listener. This extends the
06 bind-gate work with the identity/commit edges from
`todo/codex-testing/under-tested-areas.md` §3.

**Non-visual**: one client, no GUI — asserts on probe exit codes and
weston-log evidence.

**What qdwin actually does (verified, matches §3 framing)**:
- NO required-field check on commit. A commit with engine + instance_id
  but no app_id, with engine only, or with no setters at all, all succeed.
  qdwin logs the unset fields as the literal `?`, e.g.
  `qdwin/secctx: committed engine=qdistro app_id=? instance_id=…`. So the
  string qdwin forwards for an unset app_id is empty/NULL (rendered `?` in
  the log) — it does not invent one.
- The context is single-use: a second `commit`, or any setter after
  `commit`, posts `WP_SECURITY_CONTEXT_V1_ERROR_ALREADY_USED` (=1)
  ("commit twice" / "set_app_id after commit").

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-secctx-probe"
```

All cases run against one normally-started compositor (allowed_uid == this
uid, so the manager bind is admitted — that gate is covered by 06).

```bash
ID=08-secctx-edges
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)
run() { XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" "$@"; }
```

## Steps

### S1 — commit with MISSING app_id is ACCEPTED

`create_listener` + `set_sandbox_engine` + `set_instance_id` + `commit`,
with NO `set_app_id`. A valid sequence; qdwin accepts it.

```bash
# `run` may exit nonzero; capture set-e-safe (lib.sh has `set -e`).
if run --commit-no-appid; then RC=0; else RC=$?; fi
if grep -qE "qdwin/secctx: committed engine=qdistro app_id=\? instance_id=secctx-probe-1" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
if [ "$RC" -eq 0 ] && [ "$LOGHIT" -eq 0 ]; then echo "S1 PASS"; else
    echo "S1 FAIL (exit=$RC want 0; committed-log hit=$LOGHIT want 0)"; fi
```

**Assert (S1):**
- `RC == 0` — the commit round-tripped with no protocol error.
- the weston log shows `committed engine=qdistro app_id=? instance_id=…`,
  proving qdwin accepted the commit and forwards an empty app_id (logged
  `?`) rather than refusing or fabricating one. HARD.

### S2 — commit with NO metadata at all is ACCEPTED

`create_listener` + `commit`, no setters. Still a valid single-use
context.

```bash
if run --commit-bare; then RC=0; else RC=$?; fi
if grep -qE "qdwin/secctx: committed engine=\? app_id=\? instance_id=\?" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
if [ "$RC" -eq 0 ] && [ "$LOGHIT" -eq 0 ]; then echo "S2 PASS"; else
    echo "S2 FAIL (exit=$RC want 0; bare-committed-log hit=$LOGHIT want 0)"; fi
```

**Assert (S2):**
- `RC == 0` — accepted.
- the weston log shows `committed engine=? app_id=? instance_id=?` (all
  unset). HARD.

### S3 — commit with only sandbox_engine is ACCEPTED

```bash
if run --commit-engine-only; then RC=0; else RC=$?; fi
if grep -qE "qdwin/secctx: committed engine=qdistro app_id=\? instance_id=\?" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
if [ "$RC" -eq 0 ] && [ "$LOGHIT" -eq 0 ]; then echo "S3 PASS"; else
    echo "S3 FAIL (exit=$RC want 0; engine-only-committed-log hit=$LOGHIT want 0)"; fi
```

**Assert (S3):**
- `RC == 0` and the log shows `engine=qdistro app_id=? instance_id=?`. HARD.

### S4 — double commit is REFUSED (`already_used`)

A complete set + commit, then a SECOND commit on the same context. The
second must post `already_used` (=1).

```bash
# exit 3 = the probe saw the first commit succeed and the second raise the
# expected already_used error; any other value is a fail.
if run --double-commit; then RC=0; else RC=$?; fi
if [ "$RC" -eq 3 ]; then echo "S4 PASS"; else
    echo "S4 FAIL (exit=$RC want 3 = already_used on 2nd commit)"; fi
```

**Assert (S4):**
- `RC == 3` — first commit clean, second commit rejected with exactly
  `WP_SECURITY_CONTEXT_V1_ERROR_ALREADY_USED` (the probe checks the code).

### S5 — setter after commit is REFUSED (`already_used`)

A complete set + commit, then `set_app_id`. The setter must post
`already_used` (=1).

```bash
if run --set-after-commit; then RC=0; else RC=$?; fi
if [ "$RC" -eq 3 ]; then echo "S5 PASS"; else
    echo "S5 FAIL (exit=$RC want 3 = already_used on post-commit setter)"; fi
```

**Assert (S5):**
- `RC == 3` — first commit clean, the post-commit `set_app_id` rejected
  with exactly `already_used`.

## Teardown

```bash
$HT/stop.sh $ID
```

## Pass criteria

All five asserts hold: S1/S2/S3 `RC==0` + the matching `committed …` log
line, S4 `RC==3` (already_used on re-commit), S5 `RC==3` (already_used on
post-commit setter).
