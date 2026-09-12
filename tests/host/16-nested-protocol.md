# 16 — qdwin_nested_v1 bind gate + nested-proxy advertise/decision edges

**What**: drive `qdwin-nested-probe` against the headless compositor to pin
qdwin's nested-compositor passthrough path (`bind_qdwin_nested_manager`,
`qdwin_nested_manager_advertise_toplevel`,
`qdwin_handle_nested_proxy_decision`; `qdwin/qdwin.c`):

- the `qdwin_nested_manager_v1` global is peer-uid filtered at bind time
  (unauthorized uid refused, authorized uid accepted);
- `advertise_toplevel` synthesises an outer-side proxy, fires the
  `configured` event AND (with a v8 shell bound) gates the proxy via
  `qdwin_shell_v1.nested_proxy_pending`;
- `nested_proxy_decision` honours allow / deny / defer, is idempotent on a
  re-decided handle, and is a silent no-op on a stale/unknown handle;
- destroying the `qdwin_nested_toplevel_v1` tears the proxy back down
  (`toplevel_removed`);
- an empty/placeholder advertise (empty pw_node + empty metadata) is
  accepted without a crash.

**Why**: a nested compositor that could bind the manager from a foreign uid,
or surface inner toplevels the admin broker would deny, would defeat the
tier-2 isolation contract (`qdwin-nested-v1.xml` "Authorisation"). The bind
gate and the synchronous `nested_proxy_pending` → `nested_proxy_decision`
handshake are the fail-closed seam: until the shell answers `allow`, the
proxy stays on the held layer (invisible). This is the headless half of the
`qdwin_nested_v1` item in `todo/codex-testing/under-tested-areas.md` §3.

**Non-visual**: asserts on probe exit codes and weston-log evidence, not
screenshots — fully scriptable.

**Single-client design (read before relying on this)**: the decision handler
(`qdwin_handle_nested_proxy_decision`) requires the issuing resource to be
the bound shell (`qdwin_shell_require_bound`). The probe therefore binds
BOTH `qdwin_shell_v1` (+ `bind_as_shell`, so it owns the shell_resource) AND
`qdwin_nested_manager_v1` on one wl_client, advertises, and answers its own
`nested_proxy_pending`. This faithfully exercises the advertise→pending→
decision state machine headlessly. What it does NOT cover:
- a SECOND real uid binding the manager — the bind-uid reject (S1) is driven
  with a foreign `--allowed-uid` instead, exactly as 06/07 do;
- real PipeWire-fed proxy pixels (Shape-A) — the compositor uses a
  placeholder curtain until a v9 shell binds real pixels (`bind_proxy_pixels`);
  proxy-pixel-feed remains a VM/integration follow-up.

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-nested-probe"
FOREIGN_UID=$(( $(id -u) + 1 ))   # any uid != this one
```

All cases start qdwin with `--no-shell` (the harness does not spawn the
bystander); the probe binds the shell role itself in every mode except
`--bind`.

## Steps

### S1 — unauthorized uid is REFUSED at manager bind

```bash
ID=16-nested-reject
$HT/start.sh $ID --no-shell --no-terminal --allowed-uid "$FOREIGN_UID" >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

# Capture nonzero set-e-safe (lib.sh has `set -e`); exit 4 = the bind was
# refused with the EXPECTED implementation error on wl_display.
if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --bind; then RC=0; else RC=$?; fi
if grep -qE "qdwin: nested bind refused uid=$(id -u) \(allowed=$FOREIGN_UID\)" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 4 ] && [ "$LOGHIT" -eq 0 ]; then echo "S1 PASS"; else
    echo "S1 FAIL (exit=$RC want 4; refused-log hit=$LOGHIT want 0)"; fi
```

**Assert (S1):** both must hold:
- `RC == 4` — bind round-trip saw the EXPECTED implementation error on
  wl_display (the gate refused it). PRIMARY client-observed signal.
- weston log contains `qdwin: nested bind refused uid=<this> (allowed=<foreign>)`
  — proves the refusal came from the peer-uid branch. HARD.

### S2 — authorized client BINDS, advertises, gets `configured` + gating

```bash
ID=16-nested-advertise
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --advertise; then RC=0; else RC=$?; fi
if grep -qF "qdwin/nested-proxy: created handle=" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$LOGHIT" -eq 0 ]; then echo "S2 PASS"; else
    echo "S2 FAIL (exit=$RC want 0; proxy-created-log hit=$LOGHIT want 0)"; fi
```

**Assert (S2):**
- `RC == 0` — the probe saw the `configured` event, a `toplevel_added`, AND
  the `nested_proxy_pending` event (a v8 shell gates the proxy; the probe
  checks the pending handle equals the added handle).
- weston log contains `qdwin/nested-proxy: created handle=…` with
  `pending=1` (held). HARD.

### S3 — `allow` decision releases the proxy

```bash
ID=16-nested-allow
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --allow; then RC=0; else RC=$?; fi
if grep -qE "nested_proxy_decision handle=[0-9]+ ALLOW" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$LOGHIT" -eq 0 ]; then echo "S3 PASS"; else
    echo "S3 FAIL (exit=$RC want 0; ALLOW-log hit=$LOGHIT want 0)"; fi
```

**Assert (S3):**
- `RC == 0` — the allow decision round-tripped with no protocol error.
- weston log shows `nested_proxy_decision handle=… ALLOW`. HARD.

### S4 — `deny` posts policy_denied on the nested toplevel

```bash
ID=16-nested-deny
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

# exit 3 = the probe saw exactly policy_denied (=1) on the originating
# qdwin_nested_toplevel_v1 resource; any other value is a fail.
if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --deny; then RC=0; else RC=$?; fi
if grep -qE "nested_proxy_decision handle=[0-9]+ DENY" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 3 ] && [ "$LOGHIT" -eq 0 ]; then echo "S4 PASS"; else
    echo "S4 FAIL (exit=$RC want 3 = policy_denied; DENY-log hit=$LOGHIT want 0)"; fi
```

**Assert (S4):**
- `RC == 3` — the deny decision posted exactly
  `QDWIN_NESTED_MANAGER_V1_ERROR_POLICY_DENIED` (=1) on the originating
  `qdwin_nested_toplevel_v1` resource (the probe checks the code AND the
  erroring interface). PRIMARY signal.
- weston log shows `nested_proxy_decision handle=… DENY`. HARD.

### S5 — `defer` keeps the proxy held without error

```bash
ID=16-nested-defer
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --defer; then RC=0; else RC=$?; fi
if grep -qE "nested_proxy_decision handle=[0-9]+ DEFER" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$LOGHIT" -eq 0 ]; then echo "S5 PASS"; else
    echo "S5 FAIL (exit=$RC want 0; DEFER-log hit=$LOGHIT want 0)"; fi
```

**Assert (S5):**
- `RC == 0` — the defer decision round-tripped, the probe's post-defer
  liveness roundtrip passed (proxy stays held, compositor fine).
- weston log shows `nested_proxy_decision handle=… DEFER`. HARD.

### S6 — stale decision on an unknown handle is a silent no-op

```bash
ID=16-nested-stale
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)
WPID=$(ht_pid_load $ID weston)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --stale-decision; then RC=0; else RC=$?; fi
if grep -qE "nested_proxy_decision handle=[0-9]+: no nested-proxy with that handle \(stale\?\)" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
if kill -0 "$WPID" 2>/dev/null; then ALIVE=0; else ALIVE=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$LOGHIT" -eq 0 ] && [ "$ALIVE" -eq 0 ]; then echo "S6 PASS"; else
    echo "S6 FAIL (exit=$RC want 0; stale-log hit=$LOGHIT want 0; alive=$ALIVE want 0)"; fi
```

**Assert (S6):**
- `RC == 0` — the stale decision raised NO protocol error and the
  compositor stayed alive (probe liveness roundtrip passed).
- weston log shows the `no nested-proxy with that handle (stale?)` line —
  proves the request reached the handler and took the stale no-op branch.
  HARD.
- `kill -0 weston` succeeds (harness-side liveness). HARD.

### S7 — repeat decision on a non-pending handle is idempotent

```bash
ID=16-nested-double
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --double-decide; then RC=0; else RC=$?; fi
if grep -qE "nested_proxy_decision handle=[0-9]+ allow: not pending \(idempotent no-op\)" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$LOGHIT" -eq 0 ]; then echo "S7 PASS"; else
    echo "S7 FAIL (exit=$RC want 0; idempotent-log hit=$LOGHIT want 0)"; fi
```

**Assert (S7):**
- `RC == 0` — the second `allow` on the already-allowed (non-pending) handle
  round-tripped with no protocol error.
- weston log shows the `allow: not pending (idempotent no-op)` line. HARD.

### S8 — destroying the nested toplevel tears down the proxy

```bash
ID=16-nested-destroy
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --destroy-order; then RC=0; else RC=$?; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ]; then echo "S8 PASS"; else
    echo "S8 FAIL (exit=$RC want 0)"; fi
```

**Assert (S8):**
- `RC == 0` — after `qdwin_nested_toplevel_v1.destroy` the probe observed a
  `toplevel_removed` whose handle equals the advertised handle, with no
  protocol error or crash (advertise→destroy ordering holds).

### S9 — empty/placeholder advertise is accepted (no crash on empty metadata)

```bash
ID=16-nested-malformed
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --malformed; then RC=0; else RC=$?; fi
if grep -qF "qdwin: nested-toplevel advertise pw_node='' input_sink=''" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$LOGHIT" -eq 0 ]; then echo "S9 PASS"; else
    echo "S9 FAIL (exit=$RC want 0; empty-advertise-log hit=$LOGHIT want 0)"; fi
```

**Assert (S9):**
- `RC == 0` — an advertise with empty pw_node + empty input_sink + empty
  app_id/title still created a proxy and fired `configured`; the probe also
  confirms it is gated (`nested_proxy_pending`). No crash on empty metadata.
- weston log shows `nested-toplevel advertise pw_node='' input_sink=''`.
  HARD.

> Note on NULL vs empty: Wayland string args here are not `allow-null`, so a
> nested compositor sending genuine NULL would be killed by libwayland's
> marshaller before reaching qdwin (a client-side protocol violation, not a
> compositor-side gate). The placeholder shape the XML documents is the
> empty string, which is what S9 exercises.

### S10 — destroying the advertiser under a LIVE view_stream releases it

The iso2/10 E2 gate. An *allowed* proxy is a legal stream source, so an
advertiser disconnect while the shell still holds an exported `view_stream`
used to leave `qdwin_view_stream::tl` dangling into the input-inject path
(qdwin `0ed786d`). Until this step, no lane combined all three — an allowed
proxy, a live dependent, and advertiser destruction. `tests/gui/22-nested-
proxy-teardown.md` is the VM sibling that adds the popup and move dependents —
with weaker oracles than this step's, and it has not been run yet; see the
follow-up.

**Exactly what this step enforces, and what it does not.** It enforces that
the proxy destroy path releases its view_stream *at all*. Codex compiled and
ran the alternatives (`todo/reviews/proxy-lane-review-r1.md`): deleting the
`qdwin_toplevel_release_dependents()` call turns this red, but moving that
call to AFTER the curtain is destroyed and `tl->view` is NULL still passes,
as does removing the `proxy_destroying` guard on the input-focus picker, and
removing only the popup branch of the shared release routine. Those three
properties are pinned by `qdwin/test_toplevel_destroy_dependents.py` (source
invariant) and `qdwin/test_proxy_destroy_behaviour.py` (ASan behavioural) —
not here. Do not read a green S10 as covering the whole of `0ed786d`.

Needs a real PipeWire output: without one qdwin answers `denied "no free
pipewire output"` and the probe exits 77 rather than asserting nothing.
`--pipewire` loads libweston's PipeWire backend and configures that output.
It requires a PipeWire daemon on the host and `start.sh` exits 8 without one
— so a missing daemon shows up as a failed `start.sh`, never as the probe's
77.

```bash
ID=16-nested-stream
$HT/start.sh $ID --no-shell --no-terminal --pipewire >/dev/null \
    || { echo "S10 ERROR (start.sh failed; exit 8 = no PipeWire daemon)"; exit 1; }
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID)

OUT=$(XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --destroy-with-stream 2>&1) \
    && RC=0 || RC=$?
printf '%s\n' "$OUT"
$HT/stop.sh $ID
case "$RC" in
    0)  echo "S10 PASS" ;;
    77) echo "S10 INCONCLUSIVE (no live dependent; see probe stderr)" ;;
    *)  echo "S10 FAIL (exit=$RC want 0)" ;;
esac
```

**Assert (S10):**
- `RC == 0`. The probe's own postconditions are: the subscribe was `approved`;
  the stream was still un-torn-down at the moment of the destroy; `torn_down`
  then fired with reason exactly `source toplevel closed`; `toplevel_removed`
  fired for **that proxy's handle** (not merely a count bump); and a later
  request still round-tripped.
- `approved` means qdwin allocated the PipeWire output, pinned the view, minted
  a token and *forked* — it does NOT prove the `qdistro-forward` child exec'd
  or works. That is enough for what is under test (the server-side stream state
  exists and points at the proxy) and is why this step can run on a host with
  no forward binary at all.
- `RC == 77` is INCONCLUSIVE, never PASS: the mode attached no live dependent
  and asserted nothing. The probe's stderr names what is missing (no pipewire
  output, no verdict within 20s).

> Verified non-vacuous by fault injection: deleting the
> `qdwin_toplevel_release_dependents()` call from `qdwin_nested_proxy_destroy`
> — i.e. the pre-`0ed786d` behaviour — turns this case red. Independently
> reproduced by codex against its own built plugin variants
> (`todo/reviews/proxy-lane-review-r1.md`).
>
> One honest caveat about running it on a developer host: `/usr/bin/qdistro-
> forward` is usually absent there, so the forked child dies immediately and
> the stream is torn down as `"forward exited"` shortly after. The destroy wins
> that race consistently (5/5 here), and the reason assertion is what
> distinguishes the two — but which code an exec-failed child produces depends
> on timing: **77** if that teardown is already dispatched when the probe
> re-checks liveness before the destroy, **RC=1** if it arrives afterwards. A
> host seeing either is looking at a missing forward binary, not a product
> regression; re-run in the VM lane, where the forward is real, before treating
> it as one.

## Teardown

`stop.sh` runs inline per case. If a case aborts before its `stop.sh`, tear
each down individually (one id per call), e.g. `$HT/stop.sh 16-nested-reject`.

## Pass criteria

All ten asserts hold: S1 `RC==4` + refused log, S2 `RC==0` + proxy-created
log, S3 `RC==0` + ALLOW log, S4 `RC==3` (policy_denied) + DENY log, S5
`RC==0` + DEFER log, S6 `RC==0` + stale-no-op log + weston alive, S7 `RC==0`
+ idempotent log, S8 `RC==0` (toplevel_removed), S9 `RC==0` + empty-advertise
log, S10 `RC==0` (stream released on advertiser destroy). On a host with no
PipeWire daemon `start.sh --pipewire` exits 8 and S10 is an **ERROR** (the case
never ran); the probe's own 77 is reserved for a compositor that started but
could not give the stream a free output. Neither is a pass.

The move-drag and chrome-popup halves of the same E2 gate need a pointer and
are therefore VM-only: `tests/gui/22-nested-proxy-teardown.md`.
