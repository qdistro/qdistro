# 06 — secctx manager bind gate

**What**: drive `qdwin-secctx-probe` against the headless compositor in
four configurations and check that qdwin's
`wp_security_context_manager_v1` bind gate (`bind_qdwin_secctx_manager`,
`qdwin/qdwin.c`) refuses an ordinary same-UID client, accepts the bound
shell client, honours the `QDWIN_SECCTX_OPEN` developer override, and does
not admit a helper executable path that lacks installed-helper integrity.
Production also admits the installed `qdistro-secctx-exec` helper path;
the positive helper path is covered by qdistro integration tests because
it depends on the installed executable location, root ownership, root
launcher ancestry, and broker launch-record rollout.

**Why**: the broker-attested secctx identity contract is only sound if
the compositor refuses manager binds from arbitrary same-session clients.
Same uid is not an authorization basis: self-asserted secctx strings from
an arbitrary process would otherwise let it impersonate another silo.

**Non-visual**: this scenario asserts on probe exit codes and weston-log
branch evidence, not screenshots. It is fully scriptable.

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-secctx-probe"
```

## Steps

### S1 — non-shell same-UID client is REFUSED

Start qdwin normally with the bystander bound as the shell. The probe runs
as the same uid, but it is a different wl_client and must be refused.

```bash
ID=06-secctx-sameuid-reject
$HT/start.sh $ID --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

# Capture nonzero exits explicitly: lib.sh runs under `set -e`, and S1
# EXPECTS the probe to fail. Exit 2 here means the global filter hid
# wp_security_context_manager_v1 from this unauthorized client before bind.
if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE"; then RC=0; else RC=$?; fi
$HT/stop.sh $ID
if [ "$RC" -eq 2 ]; then echo "S1 PASS"; else
    echo "S1 FAIL (exit=$RC want 2)"; fi
```

**Assert (S1):**
- `RC == 2` — the probe did not see `wp_security_context_manager_v1`,
  proving the global filter hid it from the non-shell same-UID client.

### S2 — bound shell client is ACCEPTED and can commit a listener

Start qdwin without the bystander. The probe binds `qdwin_shell_v1`,
claims the shell role on that same wl_client, then binds the secctx
manager and completes `create_listener` + `commit`.

```bash
ID=06-secctx-shell-allow
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --as-shell --commit; then RC=0; else RC=$?; fi
if grep -qF "secctx: manager bound" "$WLOG" && grep -qF "(shell)" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$LOGHIT" -eq 0 ]; then echo "S2 PASS"; else
    echo "S2 FAIL (exit=$RC want 0; shell-bound-log hit=$LOGHIT want 0)"; fi
```

**Assert (S2):**
- `RC == 0` — bind accepted, and `create_listener` + secctx setters +
  `commit` round-tripped with no protocol error.
- the weston log contains `qdwin/secctx: manager bound by uid=... pid=...
  (shell)`.

### S3 — `QDWIN_SECCTX_OPEN=1` dev override admits an otherwise-refused client

Start qdwin without a shell and set the documented developer escape
hatch. The ordinary non-shell probe can bind and commit only because the
override is active.

```bash
ID=06-secctx-open
QDWIN_SECCTX_OPEN=1 $HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --commit; then RC=0; else RC=$?; fi
if grep -qF "(dev-open)" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$LOGHIT" -eq 0 ]; then echo "S3 PASS"; else
    echo "S3 FAIL (exit=$RC want 0; dev-open-log hit=$LOGHIT want 0)"; fi
```

**Assert (S3):**
- `RC == 0` — with `QDWIN_SECCTX_OPEN=1` the gate is bypassed and the
  otherwise-refused non-shell client can bind and commit.
- the weston log marks the bind as `(dev-open)`.

### S4 — helper executable path without installed-helper integrity is REFUSED

Start qdwin without a shell and point the helper-executable override at
the probe itself. This proves a matching helper path alone is not enough:
the helper executable must also be root-owned and not group/world-writable
before the root-parent test can matter.

```bash
ID=06-secctx-helper-no-root-parent
QDWIN_ALLOWED_SECCTX_HELPER_EXE="$PROBE" $HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE"; then RC=0; else RC=$?; fi
if grep -qF "executable is not root-owned" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 2 ] && [ "$LOGHIT" -eq 0 ]; then echo "S4 PASS"; else
    echo "S4 FAIL (exit=$RC want 2; helper-integrity-log hit=$LOGHIT want 0)"; fi
```

**Assert (S4):**
- `RC == 2` — the global filter hid the manager from a process whose
  executable matched `QDWIN_ALLOWED_SECCTX_HELPER_EXE` but was not a
  root-owned, non-group/world-writable installed helper.
- the weston log contains `executable is not root-owned`.

## Teardown

`stop.sh` runs inline per case. If a case aborts before its `stop.sh`,
tear each one down individually:
`$HT/stop.sh 06-secctx-sameuid-reject`,
`$HT/stop.sh 06-secctx-shell-allow`, `$HT/stop.sh 06-secctx-open`,
`$HT/stop.sh 06-secctx-helper-no-root-parent`.

## Pass criteria

All four asserts hold: S1 `RC==2`, S2 `RC==0` + shell-bound log,
S3 `RC==0` + dev-open log, S4 `RC==2` + helper-integrity rejection log.
