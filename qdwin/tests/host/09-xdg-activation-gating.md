# 09 — xdg-activation unknown-token gating

**What**: drive `qdwin-activation-probe` against the headless compositor to
check qdwin's `xdg_activation_v1` activate path
(`qdwin_activation_activate`, `qdwin/qdwin.c`) with an unknown/forged
token: qdwin logs `xdg-activation activate with unknown token` and drops
the request with no focus change and no crash.

**Why**: `activate(token, surface)` is the request that can steal focus. An
attacker that guesses or forges a token must not be able to drive
activation. qdwin looks the token up (`qdwin_activation_token_find`) and,
on miss, returns early without performing anything. This covers the
negative case the existing `xdg-activation-policy` static test
(`qdwin/test_xdg_activation_policy.py`, which only checks the
alloc-failure branch fails closed) does not exercise at runtime — see
`todo/codex-testing/under-tested-areas.md` §3.

**Non-visual**: asserts on probe exit codes and weston-log evidence — no
screenshots. The probe also does a post-activate liveness roundtrip so a
crash/wedge would surface as a nonzero exit.

**Scope notes (read before relying on this)**:
- This proves the unknown-token DROP path (the forged-token attack
  surface). It does not exercise a *valid* token reaching the broker-gated
  cross-silo decision (`qdwin_shell_can_receive_v12` → `activation_pending`
  → `activation_decision`): that needs the shell client speaking v12 and a
  mapped target toplevel. Cross-silo activation DENIAL between two tagged
  clients is a follow-up — feasible with two tagged probes + a v12 shell,
  but out of scope for this single-client headless drop test (noted as a
  follow-up).
- The probe activates against a roleless `wl_surface` (no xdg_toplevel),
  so even a *valid* token would hit qdwin's "target surface has no
  toplevel" branch — which is why we test the unknown-token branch (it
  returns BEFORE the toplevel lookup) for an unambiguous drop signal.

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-activation-probe"
ID=09-xdg-activation
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)
WPID=$(ht_pid_load $ID weston)
run() { XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" "$@"; }
```

## Steps

### S1 — a forged token is dropped, compositor stays alive

```bash
# Each unknown-token activate appends one "unknown token" log line; assert
# the COUNT grows by exactly the number of activates, so a regression that
# silently dropped without logging would fail.
UTOK="xdg-activation activate with unknown token"
PRE=$(grep -cF "$UTOK" "$WLOG" || true)
# `run` exits 0 on success; capture set-e-safe regardless (lib.sh `set -e`).
if run; then RC=0; else RC=$?; fi
POST=$(grep -cF "$UTOK" "$WLOG" || true)
if [ "$RC" -eq 0 ] && [ "$POST" -eq $((PRE + 1)) ]; then echo "S1 PASS"; else
    echo "S1 FAIL (exit=$RC want 0; unknown-token-log $PRE->$POST want +1)"; fi
```

**Assert (S1):**
- `RC == 0` — `activate(forged)` round-tripped with no protocol error AND
  the probe's post-activate liveness roundtrip succeeded (compositor still
  serving). The probe exits nonzero if either fails.
- the weston log contains `xdg-activation activate with unknown token` —
  proves the request reached the activate handler and took the drop
  branch. HARD.

### S2 — the empty token is also dropped (alloc-fail token shape)

qdwin issues only non-empty tokens, and its alloc-failure path issues `""`
(which `qdwin_activation_token_find` can never match). An activate with an
empty token therefore takes the same unknown-token drop path.

```bash
PRE=$(grep -cF "$UTOK" "$WLOG" || true)
if run --empty; then RC=0; else RC=$?; fi
POST=$(grep -cF "$UTOK" "$WLOG" || true)
if [ "$RC" -eq 0 ] && [ "$POST" -eq $((PRE + 1)) ]; then echo "S2 PASS"; else
    echo "S2 FAIL (exit=$RC want 0; unknown-token-log $PRE->$POST want +1)"; fi
```

**Assert (S2):**
- `RC == 0` — empty-token activate dropped, compositor alive.
- the unknown-token log count grew by exactly 1 (the empty token took the
  same drop path). HARD.

### S3 — repeated forged tokens don't leak/crash

Fire several forged-token activates in one client; each must be dropped
cleanly and the compositor must remain alive afterwards.

```bash
PRE=$(grep -cF "$UTOK" "$WLOG" || true)
if run --repeat=5; then RC=0; else RC=$?; fi
POST=$(grep -cF "$UTOK" "$WLOG" || true)
# Liveness double-check from the harness side, independent of the probe.
if kill -0 "$WPID" 2>/dev/null; then ALIVE=0; else ALIVE=1; fi
if [ "$RC" -eq 0 ] && [ "$ALIVE" -eq 0 ] && [ "$POST" -eq $((PRE + 5)) ]; then echo "S3 PASS"; else
    echo "S3 FAIL (exit=$RC want 0; weston-alive=$ALIVE want 0; unknown-token-log $PRE->$POST want +5)"; fi
```

**Assert (S3):**
- `RC == 0` — all five activates dropped cleanly, probe liveness roundtrip
  passed.
- the unknown-token log count grew by exactly 5 (each forged token logged
  + dropped, no silent swallow). HARD.
- `kill -0 weston` succeeds — the compositor process is still running
  after the burst (harness-side liveness, independent of the probe). HARD.

### S4 — a legitimately-issued token is single-use (replay is dropped)

The token lifecycle/expiry edge from §3: a token obtained through the full
`get_activation_token` + `commit` flow is non-empty, is CONSUMED on the first
`activate`, and a REPLAY of the same token then takes the unknown-token drop
path. Proves an attacker can't capture-and-replay a once-valid token.

```bash
ISSUED="qdwin: xdg-activation token issued"
NOTOP="qdwin: xdg-activation target surface has no toplevel"
PRE_UTOK=$(grep -cF "$UTOK" "$WLOG" || true)
PRE_NOTOP=$(grep -cF "$NOTOP" "$WLOG" || true)
PRE_ISSUED=$(grep -cF "$ISSUED" "$WLOG" || true)
if run --token-lifecycle; then RC=0; else RC=$?; fi
POST_UTOK=$(grep -cF "$UTOK" "$WLOG" || true)
POST_NOTOP=$(grep -cF "$NOTOP" "$WLOG" || true)
POST_ISSUED=$(grep -cF "$ISSUED" "$WLOG" || true)
if kill -0 "$WPID" 2>/dev/null; then ALIVE=0; else ALIVE=1; fi
if [ "$RC" -eq 0 ] && [ "$ALIVE" -eq 0 ] \
   && [ "$POST_ISSUED" -eq $((PRE_ISSUED + 1)) ] \
   && [ "$POST_NOTOP" -eq $((PRE_NOTOP + 1)) ] \
   && [ "$POST_UTOK" -eq $((PRE_UTOK + 1)) ]; then echo "S4 PASS"; else
    echo "S4 FAIL (exit=$RC want 0; alive=$ALIVE want 0; issued $PRE_ISSUED->$POST_ISSUED want +1; no-toplevel $PRE_NOTOP->$POST_NOTOP want +1; unknown-token $PRE_UTOK->$POST_UTOK want +1)"; fi
```

**Assert (S4):**
- `RC == 0` — the probe got a NON-EMPTY token from `done`, the first
  `activate` round-tripped, and the replay round-tripped (no protocol error).
- the `token issued` log count grew by exactly 1 — a real token was minted.
  HARD.
- the `target surface has no toplevel` count grew by exactly 1 — the FIRST
  activate found the (real) token and consumed it, then bailed because the
  roleless surface has no toplevel (a valid token still can't steal focus
  onto a roleless surface). HARD.
- the `unknown token` count grew by exactly 1 — the SECOND activate of the
  SAME token found nothing, proving the token was single-use (consumed on
  first use, not replayable). HARD.
- `kill -0 weston` succeeds. HARD.

## Teardown

```bash
$HT/stop.sh $ID
```

## Pass criteria

All four asserts hold: S1 `RC==0` + unknown-token log, S2 `RC==0`, S3 `RC==0`
+ weston still alive, S4 `RC==0` + token-issued/no-toplevel/unknown-token
counts each +1 (legitimate token is single-use; replay dropped).
