# 25 — virtual-keyboard-unstable-v1 (gated manager + key injection)

**What**: pin qdwin's `zwp_virtual_keyboard_manager_v1` — the privileged
key-injection companion of input-method-v2 (Bucket A / P1;
`bind_qdwin_virtual_keyboard_manager`,
`qdwin_vk_manager_create_virtual_keyboard`, the `keymap`/`key`/`modifiers`
request handlers; `qdwin/qdwin.c`):

- `zwp_virtual_keyboard_manager_v1` is advertised and binds **from the authorized
  session client** (unsandboxed, `allowed_ime_uid`) — it is GATED via the SAME
  `qdwin_ime_family_bind_allowed` helper as input-method-v2;
- a sandboxed/secctx client cannot even see the global (hidden by the wl_global
  filter) and is rejected at bind;
- `create_virtual_keyboard(seat)` needs a `wl_seat`; under a seat, a virtual
  keyboard that re-uploads the seat's own keymap (as a real IME does) can inject
  `key` + `modifiers` with no protocol error;
- injecting a `key` BEFORE a keymap is the protocol `no_keymap` error
  (fail-closed contract).

**Why**: a grabbing IME (fcitx5/ibus) composes some keys and passes the rest
back to the focused app by injecting them through a virtual keyboard. Without
this companion a grabbing IME swallows passthrough keys — so it is a hard
prerequisite before enabling a real IME in production (see
`todo/open-followups.md`). Because a virtual keyboard injects arbitrary
keystrokes into whatever app is focused, the gating (silo apps must never obtain
one) is the load-bearing security property — identical to input-method-v2. This
is the seat-gated, live half; the fully-headless source invariants are
`meson test virtual-keyboard-gating` (`qdwin/test_virtual_keyboard.py`).

**Non-visual**: asserts on probe exit codes + the static-invariant meson test.
(Whether an injected key actually produces a character in a focused app is a VM
functional test needing a paired focused client; this probe proves the protocol
exchange.)

## Two-part coverage (read first)

`create_virtual_keyboard` needs a `wl_seat`. The weston **headless** backend has
no input backend and advertises no `wl_seat`, so the live inject assertions are
reachable only under a seat-bearing backend (a VM, or weston's RDP/DRM/wayland
backend). Coverage splits:

1. **Static invariants — fully headless, always run** (`meson test
   virtual-keyboard-gating`, `qdwin/test_virtual_keyboard.py`): the manager is
   created GATED at v1 via the shared bind gate fail-closed, the global filter
   hides it from silos, key/modifiers are `no_keymap`-guarded, injection goes via
   `notify_key`/`notify_modifiers` without swapping the seat keymap, the manager
   is neutralizable, and seat-destroy + teardown detach cleanly.
2. **Live probe — seat-gated** (`qdwin-vkbd-probe`): the manager-advertised +
   no-seat gate (exit 5) runs headless; the default inject mode and `--nokeymap`
   PASS under a seat (VM), SKIP headless.

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-vkbd-probe"
```

## Steps

### S-static — source invariants (always runs, no seat needed)

```bash
export PATH="$HOME/.local/bin:$PATH"
meson test -C "${QDWIN_BUILD:-build}" virtual-keyboard-gating
```

**Assert (S-static):** exit 0, `PASS: virtual-keyboard-v1 ...`. Fails if the
manager loses its bind gate, the secctx-deny is removed from the filter or the
shared helper, the `no_keymap` guard is dropped, injection stops going through
the seat, or teardown stops detaching.

### S0 — manager advertised; functional path gated by seat presence (always runs)

```bash
ID=25-vk
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID)
run() { XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" "$@"; }

if run; then RC=0; else RC=$?; fi
SEAT_PRESENT=0
[ "$RC" -ne 5 ] && SEAT_PRESENT=1
if [ "$RC" -eq 5 ]; then
    echo "S0 PASS (headless: zwp_virtual_keyboard_manager_v1 advertised + bound, no seat — inject path is VM-only)"
elif [ "$RC" -eq 0 ]; then
    echo "S0 PASS (seat present: bound, keymap + key + modifiers injected, no protocol error)"
else
    echo "S0 FAIL (exit=$RC; want 5 headless or 0 with a seat)"
fi
```

**Assert (S0):** `RC == 5` headless (manager advertised + bound, no seat), or
`RC == 0` under a seat (inject exchange held). Any other exit fails.

### S1 — no_keymap fail-closed contract (seat-gated; PASS in VM, SKIP headless)

```bash
if [ "$SEAT_PRESENT" = 1 ]; then
    if run --nokeymap; then RC=0; else RC=$?; fi
    if [ "$RC" -eq 0 ]; then echo "S1 PASS"; else echo "S1 FAIL (exit=$RC want 0)"; fi
else
    echo "S1 SKIP (no seat headless — run in a VM/RDP backend)"
fi
$HT/stop.sh $ID
```

**Assert (S1, when a seat is present):** `RC == 0` — a `key` before any keymap
raised a protocol error (`no_keymap`), proving the fail-closed contract.

### S2 — registry audit (VM, with the full session)

Run `tests/gui/agent-protocol-audit.sh` in the VM; it now requires
`zwp_virtual_keyboard_manager_v1` in the live registry alongside
`zwp_input_method_manager_v2` / `zwp_text_input_manager_v3`.

## Teardown

`stop.sh $ID` runs at the end of S1. If a case aborts earlier, tear down with
`$HT/stop.sh 25-vk`.

## Pass criteria

S-static `exit 0`; S0 `RC==5` headless (or `0` with a seat); S1 PASS in a
seat-bearing backend (documented SKIP headless); S2 audit lists the global.
