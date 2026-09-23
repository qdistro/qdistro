# 07 — qdwin_locker_v1 bind gate (uid gate + one-locker-at-a-time)

**What**: drive `qdwin-locker-probe` against the headless compositor to
check qdwin's `qdwin_locker_v1` bind gate (`bind_qdwin_locker` +
`qdwin_handle_bind_as_locker`, `qdwin/qdwin.c`): an unauthorized uid is
refused at global bind, an authorized client binds and gets `ready`, and
the "only one locker may exist at a time" semantics in the XML hold
(double `bind_as_locker` on one resource is rejected with `already_bound`;
a second locker bind while the first peer is alive is REFUSED with
`locker_present` — the live locker is NOT evicted, both same-pid (S4) and
across two independent same-uid PROCESSES (S6)). It also checks the
production default makes the locker ENTRYPOINT identity mandatory (S5,
FINDING #5).

**Why**: the locker is the only client allowed to drive compositor `locked`
state. If an arbitrary same-session client could bind the locker global, it
could black-screen the session or, worse, swap the lock UI. The gate keys
on the peer uid (`uid != allowed_locker_uid` → implementation error) with
optional defence-in-depth exe/SELinux checks (df8f3d5). This scenario is
the headless half of the locker bind-gate item in
`todo/codex-testing/under-tested-areas.md` §3.

**Non-visual**: asserts on probe exit codes and weston-log evidence, not
screenshots — fully scriptable.

**Headless / scope notes (read before relying on this)**:
- `allowed_locker_uid` has no separate CLI/env knob today: qdwin defaults
  it to `(uid_t)-1` and falls back to `allowed_uid` at init
  (`qdwin.c`, search `allowed_locker_uid == (uid_t)-1`). So a foreign
  `QDWIN_ALLOWED_UID` (via `start.sh --allowed-uid`) is exactly what makes
  the locker uid gate reject — no `--allowed-locker-uid` was needed.
- A single-uid host cannot run a client under a second real uid, so the
  "unauthorized" case is driven with a foreign `allowed_uid` + `--no-shell`
  (the probe's real uid then mismatches `allowed_locker_uid`). This
  faithfully exercises the uid reject branch.
- The locker policy now defaults to a MANDATORY ENTRYPOINT policy when no
  explicit exe/label/entrypoint is configured (FINDING #5(a) fail-closed):
  the peer exe must be a system interpreter (python under a trusted bindir)
  AND `argv[1]` must realpath to a canonical, root-owned entrypoint
  (`/usr/local/bin/qdlocker` or `/usr/bin/qdlocker`). This matches how the
  real qdlocker (a Python console-script) actually appears in `/proc` — the
  earlier exe-only default (`exe==/usr/bin/qdlocker`) could NEVER match a
  script launcher and would have bricked the lockscreen. The headless probe
  is a native ELF (not a python-launched entrypoint), so `start.sh` exports
  `QDWIN_ALLOWED_LOCKER_ANY=1` to drop to the legacy uid-only policy for the
  uid-gate positive paths (S2–S4, S6). S5 overrides that to verify the
  production mandatory-entrypoint default itself. The entrypoint MATCH path
  (a real python-launched qdlocker accepted) is VM-validated separately
  (see `tests/gui/` / the review doc).
- Overlay-key routing while locked and lock-surface destroy/recreate need a
  real lock cycle + input injection — left as VM follow-ups (see
  `tests/gui/`), not attempted headless.

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-locker-probe"
FOREIGN_UID=$(( $(id -u) + 1 ))   # any uid != this one
```

## Steps

### S1 — unauthorized uid is REFUSED at bind

Start qdwin with a foreign allowed_uid and no shell, so the probe's real
uid matches neither `allowed_locker_uid` nor the shell.

```bash
ID=07-locker-reject
$HT/start.sh $ID --no-shell --no-terminal --allowed-uid "$FOREIGN_UID" >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

# lib.sh runs under `set -e` and S1 EXPECTS a nonzero probe exit, so
# capture it set-e-safe (a bare `cmd; RC=$?` would abort the script). The
# probe exits 4 ONLY when the bind was refused with the expected
# implementation error on wl_display (a wrong/unexpected error exits 1), so
# RC==4 already rules out an unrelated disconnect.
if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE"; then RC=0; else RC=$?; fi
# HONEST LOG NOTE: the uid reject is posted to the CLIENT as an
# implementation error ("uid N not permitted") — unlike secctx (06) the
# locker has NO dedicated "REJECTED" weston-log line. The only server-log
# evidence is the bind-attempt line showing the uid mismatch, plus the
# ABSENCE of a "locker bound" line (no binding was created). Assert both:
# a mismatched bind-attempt line is present AND no "locker bound" appeared.
if grep -qE "locker bind attempt .* uid=$(id -u) .*allowed_locker_uid=$FOREIGN_UID" "$WLOG"; then ATTEMPT=0; else ATTEMPT=1; fi
if grep -qF "qdwin: locker bound" "$WLOG"; then BOUND=1; else BOUND=0; fi
$HT/stop.sh $ID
if [ "$RC" -eq 4 ] && [ "$ATTEMPT" -eq 0 ] && [ "$BOUND" -eq 0 ]; then echo "S1 PASS"; else
    echo "S1 FAIL (exit=$RC want 4; attempt-log=$ATTEMPT want 0; bound-log=$BOUND want 0)"; fi
```

**Assert (S1):** all must hold (the conjunction is the pass — a bind error
alone could be an unrelated disconnect):
- `RC == 4` — the probe's bind round-trip saw the EXPECTED implementation
  error on wl_display (gate refused the bind). A wrong/unexpected error
  exits 1 and fails. PRIMARY client-observed signal.
- the weston log contains a `locker bind attempt … uid=<this> …
  allowed_locker_uid=<foreign>` line — proves the request reached the uid
  gate with a real mismatch. HARD.
- the weston log contains NO `qdwin: locker bound` line — proves no
  binding was created. HARD. (The "uid N not permitted" text is posted to
  the client, not the server log, so it is observed via `RC` instead.)

### S2 — authorized client is ACCEPTED (gets `ready`)

Start qdwin normally (allowed_uid == this uid). The probe binds and
`bind_as_locker` must succeed (the `ready` event fires).

```bash
ID=07-locker-allow
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE"; then RC=0; else RC=$?; fi
if grep -qF "qdwin: locker bound" "$WLOG"; then LOGHIT=0; else LOGHIT=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$LOGHIT" -eq 0 ]; then echo "S2 PASS"; else
    echo "S2 FAIL (exit=$RC want 0; bound-log hit=$LOGHIT want 0)"; fi
```

**Assert (S2):**
- `RC == 0` — bind accepted AND `bind_as_locker` round-tripped with the
  `ready` event delivered (the probe checks for it explicitly).
- the weston log contains `qdwin: locker bound pid=… uid=…` (HARD).

### S3 — second `bind_as_locker` on the same resource is REJECTED (`already_bound`)

The locker XML: "qdwin rejects a second bind from the same client". The
probe binds once, then calls `bind_as_locker` twice on the same resource;
the second must raise the dedicated `already_bound` (=1) error.

```bash
ID=07-locker-double
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID)

# exit 3 = the expected already_bound rejection (probe's PASS signal); any
# other value (incl. a generic exit 1 from a *different* error) is a fail.
if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --double-bind; then RC=0; else RC=$?; fi
$HT/stop.sh $ID
if [ "$RC" -eq 3 ]; then echo "S3 PASS"; else
    echo "S3 FAIL (exit=$RC want 3 = already_bound)"; fi
```

**Assert (S3):**
- `RC == 3` — the probe saw the FIRST `bind_as_locker` succeed (`ready`)
  and the SECOND raise exactly `QDWIN_LOCKER_V1_ERROR_ALREADY_BOUND` (=1).
  The probe distinguishes this from any other protocol error.

### S4 — a second locker bind is REFUSED while the first peer is ALIVE (FINDING #5)

Security fix: qdwin must NOT evict a live locker just because another
same-uid client binds. The probe binds the global twice (two resources on
one client — same live pid) and calls `bind_as_locker` on each. The first
becomes the live locker; because the probe process (the locker peer) is
still alive, the second `bind_as_locker` must be REFUSED with the
dedicated `locker_present` (=5) error and the original binding left
intact. Previously qdwin destroyed the old binding here, letting any
same-uid client take over the locker and call `set_locked(0)`.

```bash
ID=07-locker-rebind
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

# exit 5 = the expected locker_present rejection (probe's PASS signal); any
# other value (incl. exit 1 from acceptance/eviction) is a fail.
if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --rebind; then RC=0; else RC=$?; fi
# Exactly ONE "locker bound" line (the first); the second was refused, so
# no replacement bound line is emitted. The refusal log line is present.
BOUND=$(grep -cF "qdwin: locker bound" "$WLOG" || true)
if grep -qF "qdwin: locker bind REFUSED" "$WLOG"; then REFUSED=0; else REFUSED=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 5 ] && [ "$BOUND" -eq 1 ] && [ "$REFUSED" -eq 0 ]; then echo "S4 PASS"; else
    echo "S4 FAIL (exit=$RC want 5; bound-count=$BOUND want 1; refused-log=$REFUSED want 0)"; fi
```

**Assert (S4):** all must hold:
- `RC == 5` — the second `bind_as_locker` raised exactly
  `QDWIN_LOCKER_V1_ERROR_LOCKER_PRESENT` (=5) on `qdwin_locker_v1`. The
  probe distinguishes this from any other protocol error and from
  acceptance. PRIMARY signal.
- exactly ONE `qdwin: locker bound` line — the live locker was NOT
  evicted/replaced. HARD.
- a `qdwin: locker bind REFUSED … takeover denied` line is present. HARD.

### S5 — mandatory-entrypoint locker policy default (FINDING #5(a))

Security fix: with no explicit exe/label/entrypoint policy, qdwin must
default to the mandatory ENTRYPOINT policy (not uid-only). qdlocker is a
Python setuptools console-script, so its `/proc/<pid>/exe` is the
interpreter (`/usr/bin/python3.N`) and the launcher path is `argv[1]`; the
default therefore requires the peer exe to be a system interpreter AND
`argv[1]` to realpath to one of the canonical, root-owned entrypoint files
(`/usr/local/bin/qdlocker` or `/usr/bin/qdlocker`, profile-dependent —
verified on the daily VM: exe→`/usr/bin/python3.13`,
argv[1]→`/usr/local/bin/qdlocker`). Start qdwin WITHOUT the dev/test opt-out
and confirm the startup log shows the entrypoint default (not
`[UID-ONLY: INSECURE]`). The probe (a native ELF whose exe is the probe
binary, not a python interpreter, and whose argv[1] is not the entrypoint)
is then rejected at bind.

```bash
ID=07-locker-defexe
QDWIN_ALLOWED_LOCKER_ANY= $HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

# Policy line must name the default entrypoint(s) and must NOT be uid-only.
if grep -qE "qdwin: locker bind policy .*entrypoint=/usr/local/bin/qdlocker" "$WLOG"; then POL=0; else POL=1; fi
if grep -qF "[UID-ONLY: INSECURE]" "$WLOG"; then UIDONLY=1; else UIDONLY=0; fi
# The probe is a native ELF (exe is not a python interpreter) and argv[1] is
# not the entrypoint, so bind_as_locker is rejected; no "locker bound" line.
if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE"; then RC=0; else RC=$?; fi
if grep -qF "qdwin: locker bound" "$WLOG"; then BOUND=1; else BOUND=0; fi
$HT/stop.sh $ID
if [ "$POL" -eq 0 ] && [ "$UIDONLY" -eq 0 ] && [ "$BOUND" -eq 0 ]; then echo "S5 PASS"; else
    echo "S5 FAIL (policy-entrypoint=$POL want 0; uid-only-flag=$UIDONLY want 0; bound=$BOUND want 0; probe-rc=$RC)"; fi
```

**Assert (S5):**
- the weston log policy line reads `entrypoint=/usr/local/bin/qdlocker…`
  (mandatory entrypoint defaulted) and NOT `[UID-ONLY: INSECURE]`. HARD.
- NO `qdwin: locker bound` line — the probe (not a python-launched
  entrypoint) was rejected, so uid-only takeover is closed. HARD.

> Note: `start.sh` exports `QDWIN_ALLOWED_LOCKER_ANY=1` by default so the
> uid-only positive paths (S2–S4) work with the test probe. S5 overrides
> it to empty to exercise the production mandatory-entrypoint default.

### S6 — separate-process takeover is REFUSED while the holder lives, ACCEPTED after it dies (FINDING #5)

The S4 `--rebind` test binds two resources on ONE process (one pid), so it
cannot exercise the cross-process liveness check in
`qdwin_locker_peer_alive`. S6 uses `--separate-takeover`: the probe forks a
CHILD that binds as the live locker and holds it, then the PARENT (a
DIFFERENT pid, its own connection) attempts `bind_as_locker`. While the
child lives the takeover must be REFUSED with `locker_present`; after the
parent kills the child and qdwin observes the unbind, a re-attempt must be
ACCEPTED (`ready`). The probe exits 6 only if BOTH halves hold.

```bash
ID=07-locker-septakeover
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

# exit 6 = refused-while-alive AND accepted-after-death (probe's PASS).
if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --separate-takeover; then RC=0; else RC=$?; fi
# While the holder lived, the takeover was refused (the live holder was NOT
# evicted). After the holder died, qdwin clears the binding from the holder's
# socket-close (a `locker unbound` line) and the parent's retry binds — so
# there are TWO distinct `locker bound` lines (holder pid, then parent pid).
if grep -qF "qdwin: locker bind REFUSED" "$WLOG"; then REFUSED=0; else REFUSED=1; fi
BOUND=$(grep -cF "qdwin: locker bound" "$WLOG" || true)
$HT/stop.sh $ID
if [ "$RC" -eq 6 ] && [ "$REFUSED" -eq 0 ] && [ "$BOUND" -ge 2 ]; then echo "S6 PASS"; else
    echo "S6 FAIL (exit=$RC want 6; refused-log=$REFUSED want 0; bound-count=$BOUND want >=2)"; fi
```

**Assert (S6):** all must hold:
- `RC == 6` — the takeover by a DISTINCT pid was refused with
  `locker_present` while the holder lived, and accepted (`ready`) only after
  the holder died. PRIMARY signal.
- a `qdwin: locker bind REFUSED … takeover denied` line (the live holder was
  not evicted). HARD.
- at least TWO `qdwin: locker bound` lines — the holder's bind, then the
  parent's post-death bind. The interleaved `qdwin: locker unbound` (the
  holder's socket close) is what frees the binding so the dead-peer
  replacement is permitted; the parent is NOT bound until the holder is gone.
  HARD.

> Note: S6 runs under the default `QDWIN_ALLOWED_LOCKER_ANY=1` from
> `start.sh` (the probe is not a python-launched entrypoint), so the
> identity gate is uid-only here and the test isolates the cross-process
> *liveness* check.

## Teardown

`stop.sh` runs inline per case. If a case aborts before its `stop.sh`,
tear each one down individually:
`$HT/stop.sh 07-locker-reject`, `$HT/stop.sh 07-locker-allow`,
`$HT/stop.sh 07-locker-double`, `$HT/stop.sh 07-locker-rebind`,
`$HT/stop.sh 07-locker-defexe`, `$HT/stop.sh 07-locker-septakeover`.

## Pass criteria

All six asserts hold: S1 `RC==4` (implementation-error reject) +
bind-attempt mismatch log + NO bound log, S2 `RC==0` + bound log, S3
`RC==3` (already_bound), S4 `RC==5` (locker_present) + exactly 1 bound
line + REFUSED log, S5 mandatory-entrypoint policy line + no uid-only flag +
no bound line, S6 `RC==6` (separate-process takeover refused while alive,
accepted after death) + REFUSED log + stale-replacement log.
