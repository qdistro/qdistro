# 19 — v19 global-hotkey registration edges (delivery half is VM-only)

**What**: drive `qdwin-hotkey-probe` against the headless compositor to pin
qdwin's `register_hotkey` / `unregister_hotkey` edges
(`qdwin_handle_register_hotkey`, `qdwin_handle_unregister_hotkey`,
`qdwin_hotkey_find`, `qdwin_hotkeys_purge`; `qdwin/qdwin.c`):

- two DISTINCT ids with the SAME (modifiers, key) both register
  deterministically (qdwin keys bindings by id — duplicates across ids are
  independent entries);
- re-registering the SAME id with a different combo REPLACES the binding
  (the handler destroys the prior entry before adding the new one);
- `unregister_hotkey` removes the binding; an unknown id is a silent no-op;
- a modifier-only registration (key=0) is a no-op that installs NO binding
  and posts NO error.

**Why**: the v19 hotkey API is how qdshell wires Super/Ctrl-combos to
launcher/switcher/etc. A non-deterministic duplicate, a leaked binding on
re-register, or an error on an unknown unregister would make the shell's
keymap state diverge from the compositor's. The existing
`qdwin/run-v19-hotkey-test.sh` locks the request surface + binding-alloc +
teardown via a python scrape; this scenario adds the per-edge behavioural
cases from `todo/codex-testing/under-tested-areas.md` §4 (duplicate
determinism, replace, unknown-id no-op, modifier-only no-op) as
weston-log-observable postconditions.

**Non-visual**: asserts on probe exit codes and weston-log
`register_hotkey`/`unregister_hotkey` lines.

## Headless limitation — real DELIVERY is VM-only (read first)

§4's headline item — "a real registered hotkey press delivers the expected
`hotkey_pressed` event" — and its siblings (unregister removes DELIVERY,
hotkeys do NOT fire while a locker/overlay grab owns input, modifier-only &
chord release ordering) all require **synthesising a real `wl_keyboard` key
event** so `weston_compositor_run_key_binding` fires `qdwin_hotkey_handler`.
That needs a seat with a keyboard. The weston **headless** backend has no
input backend and exposes no `wl_seat` (verified — see
tests/host/17-cursor-shape.md), and `weston-test` / `ext-virtual-pointer-v1`
are not packaged on this host. So real keypress delivery and grab-suppression
are **VM-only** (alongside `tests/gui/15-keybinding-events.md`).

This scenario therefore pins the REGISTER state machine — which IS reachable
headless (pure shell requests + server-log evidence) — and documents the
delivery half as the VM follow-up. The grab-suppression contract (hotkeys
absorbed while a locker/overlay grab is active) is structurally guaranteed by
`weston_compositor_add_key_binding` (bindings run only in the default
keyboard grab, not under an overlay/lock grab — qdwin.c v19 comment); proving
it end-to-end needs the same key injection and is part of the VM follow-up.

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-hotkey-probe"
```

Each case uses its own fresh compositor (so the weston log holds only that
case's hotkey lines), matching the 06/07 per-case idiom.

## Steps

### S1 — duplicate (mods,key) across two ids both register

```bash
ID=19-hk-dup
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --dup-across-ids; then RC=0; else RC=$?; fi
# Both ids must appear with the SAME combo (Ctrl=0x1, KEY_E=18).
N1=$(grep -cE "register_hotkey id=1 mods=0x1 key=18" "$WLOG" || true)
N2=$(grep -cE "register_hotkey id=2 mods=0x1 key=18" "$WLOG" || true)
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$N1" -ge 1 ] && [ "$N2" -ge 1 ]; then echo "S1 PASS"; else
    echo "S1 FAIL (exit=$RC want 0; id1-log=$N1 id2-log=$N2 want >=1 each)"; fi
```

**Assert (S1):**
- `RC == 0` — both registers round-tripped with no protocol error.
- weston log has BOTH `register_hotkey id=1 mods=0x1 key=18` AND
  `register_hotkey id=2 mods=0x1 key=18` — proving the same combo under two
  ids produces two independent, deterministic bindings (not a collision or a
  dropped second register). HARD.

### S2 — re-registering an id replaces its binding

```bash
ID=19-hk-replace
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --replace; then RC=0; else RC=$?; fi
OLD=$(grep -cE "register_hotkey id=1 mods=0x1 key=18" "$WLOG" || true)
NEW=$(grep -cE "register_hotkey id=1 mods=0x2 key=38" "$WLOG" || true)
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$OLD" -ge 1 ] && [ "$NEW" -ge 1 ]; then echo "S2 PASS"; else
    echo "S2 FAIL (exit=$RC want 0; old-combo=$OLD new-combo=$NEW want >=1 each)"; fi
```

**Assert (S2):**
- `RC == 0` — both registers round-tripped.
- weston log shows the OLD combo (`id=1 mods=0x1 key=18`) AND the NEW combo
  (`id=1 mods=0x2 key=38`) for the same id — proving the re-register replaced
  the binding (the handler logged both the original and the replacement;
  the prior `weston_binding` was destroyed before the new one was added). HARD.

### S3 — unregister removes the binding; unknown id is a silent no-op

```bash
ID=19-hk-unreg
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)
WPID=$(ht_pid_load $ID weston)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --unregister; then RC=0; else RC=$?; fi
REG=$(grep -cE "register_hotkey id=1 mods=0x4 key=57" "$WLOG" || true)
UNREG=$(grep -cE "unregister_hotkey id=1" "$WLOG" || true)
# The unknown id=99 must NOT produce an unregister line (qdwin only logs on a
# real hit; a silent no-op leaves no trace) and must NOT crash the server.
UNK=$(grep -cE "unregister_hotkey id=99" "$WLOG" || true)
if kill -0 "$WPID" 2>/dev/null; then ALIVE=0; else ALIVE=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$REG" -ge 1 ] && [ "$UNREG" -ge 1 ] && [ "$UNK" -eq 0 ] && [ "$ALIVE" -eq 0 ]; then echo "S3 PASS"; else
    echo "S3 FAIL (exit=$RC want 0; reg=$REG want >=1; unreg=$UNREG want >=1; unknown-log=$UNK want 0; alive=$ALIVE want 0)"; fi
```

**Assert (S3):**
- `RC == 0` — register + unregister(id=1) + unregister(unknown id=99) all
  round-tripped.
- weston log has `register_hotkey id=1 mods=0x4 key=57` AND
  `unregister_hotkey id=1` — the binding was installed then removed. HARD.
- weston log has NO `unregister_hotkey id=99` line — qdwin logs only on a real
  removal, so the unknown-id call was the documented silent no-op. HARD.
- `kill -0 weston` succeeds — the no-op didn't crash the compositor. HARD.

### S4 — modifier-only (key=0) is a clean no-op

```bash
ID=19-hk-modonly
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --modonly; then RC=0; else RC=$?; fi
# key=0 returns BEFORE the register log line, so NO register line for it.
NREG=$(grep -cE "register_hotkey id=1 .* key=0$" "$WLOG" || true)
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$NREG" -eq 0 ]; then echo "S4 PASS"; else
    echo "S4 FAIL (exit=$RC want 0; key=0 register-log=$NREG want 0)"; fi
```

**Assert (S4):**
- `RC == 0` — the `register_hotkey(id=1, Ctrl, key=0)` round-tripped with NO
  protocol error (the XML: modifier-only combos post no error, the call is a
  no-op). PRIMARY signal.
- weston log has NO `register_hotkey … key=0` line — proving the handler took
  the `key == 0` early-return before installing/logging a binding (no leaked
  binding for an unusable combo). HARD.

## Teardown

`stop.sh` runs inline per case. If a case aborts before its `stop.sh`, tear
each down individually, e.g. `$HT/stop.sh 19-hk-dup`.

## Pass criteria

All four register-edge asserts hold: S1 (dup across ids → two register
lines), S2 (replace → old+new combo for one id), S3 (register+unregister +
unknown-id silent no-op + weston alive), S4 (modifier-only no-op, no register
line). The real keypress→`hotkey_pressed` delivery, unregister-removes-
delivery, locker/overlay grab suppression, and chord/modifier-only RELEASE
ordering remain the VM follow-up (input injection required).
