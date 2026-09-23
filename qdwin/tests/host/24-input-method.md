# 24 — input-method-unstable-v2 (gated manager + one-per-seat + keyboard grab)

**What**: pin qdwin's `zwp_input_method_manager_v2` — the PRIVILEGED IME side of
the text-input plane (Bucket A / P1; `bind_qdwin_input_method_manager`,
`qdwin_im_manager_get_input_method`, the keyboard-grab forwarding, and the
IME→text-input commit bridge; `qdwin/qdwin.c`):

- `zwp_input_method_manager_v2` is advertised and binds **from the authorized
  session client** (unsandboxed, `allowed_ime_uid`) — it is GATED, not open
  like `zwp_text_input_manager_v3`;
- a sandboxed/secctx client cannot even see the global (hidden by the wl_global
  filter) and is rejected at bind;
- `get_input_method(seat)` needs a `wl_seat`; one IME per seat — a **second**
  `get_input_method` on a seat that already has one gets exactly one
  `unavailable` event (per protocol), never an implementation error;
- `grab_keyboard` delivers a `keymap` (fd + size) and a `repeat_info` to the
  grab object before any key, proving the compositor wired the grab to the seat
  keyboard.

**Why**: toolkits speak `zwp_text_input_v3`, but actual composition (CJK /
complex script via fcitx5 / ibus) needs an input method bound to
`zwp_input_method_manager_v2` that grabs the keyboard and injects composed text.
Because that grants keystroke capture + text injection, the gating (silo apps
must never become the IME) is the load-bearing security property. This is the
seat-gated, live half of the IME item in
`todo/issues/qdwin/app-compat-protocol-gaps.md` (P1); the fully-headless source
invariants are `meson test input-method-gating` (`qdwin/test_input_method.py`).

> **Note — full CJK end-to-end needs `virtual-keyboard-v1`.** A grabbing IME
> (fcitx5/ibus) forwards non-composed keys back to apps via
> `zwp_virtual_keyboard_manager_v1`, which qdwin does not yet advertise. Until
> that companion lands (see `todo/open-followups.md`), only a deliberately
> launched, gated IME ever grabs; the default (no IME bound) keeps text-input-v3
> inert and the keyboard untouched. So this probe verifies the IME *plumbing*
> (bind gate, one-per-seat, grab keymap delivery), not a full fcitx5 CJK round.

**Non-visual**: asserts on probe exit codes + the static-invariant meson test.

## Two-part coverage (read first)

`get_input_method` needs a `wl_seat`. The weston **headless** backend has no
input backend and advertises no `wl_seat`, so the live get_input_method / grab
assertions are reachable only under a seat-bearing backend (a VM, or weston's
RDP/DRM/wayland backend). Coverage splits:

1. **Static invariants — fully headless, always run** (`meson test
   input-method-gating`, `qdwin/test_input_method.py`): the manager is created
   GATED at v1, the bind rejects sandboxed/uid-mismatched clients fail-closed,
   the global filter hides it from silos, one-IME-per-seat uses `unavailable`,
   the keyboard grab suppresses app delivery, the commit is serial/activation
   guarded, and seat-destroy + teardown detach cleanly.
2. **Live probe — seat-gated** (`qdwin-imethod-probe`): the manager-advertised +
   no-seat gate (exit 5) runs headless; `--bind` (one-per-seat `unavailable`)
   and `--grab` (keymap + repeat_info) PASS under a seat (VM), SKIP headless.

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-imethod-probe"
```

## Steps

### S-static — source invariants (always runs, no seat needed)

```bash
export PATH="$HOME/.local/bin:$PATH"
meson test -C "${QDWIN_BUILD:-build}" input-method-gating
```

**Assert (S-static):** exit 0, `PASS: input-method-v2 ...`. Fails if the manager
loses its bind gate, the secctx-deny is removed from the filter or bind, the
one-per-seat `unavailable` path changes, the grab starts double-delivering keys,
or teardown stops detaching.

### S0 — manager advertised; functional path gated by seat presence (always runs)

```bash
ID=24-im
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID)
run() { XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" "$@"; }

if run --bind; then RC=0; else RC=$?; fi
SEAT_PRESENT=0
[ "$RC" -ne 5 ] && SEAT_PRESENT=1
if [ "$RC" -eq 5 ]; then
    echo "S0 PASS (headless: zwp_input_method_manager_v2 advertised + bound, no seat — functional path is VM-only)"
elif [ "$RC" -eq 0 ]; then
    echo "S0 PASS (seat present: bound, sole IME active, second IME told unavailable)"
else
    echo "S0 FAIL (exit=$RC; want 5 headless or 0 with a seat)"
fi
```

**Assert (S0):** `RC == 5` headless (manager advertised + bound, no seat), or
`RC == 0` under a seat (one-per-seat `unavailable` held). Any other exit fails.

### S1 — keyboard grab keymap delivery (seat-gated; PASS in VM, SKIP headless)

```bash
if [ "$SEAT_PRESENT" = 1 ]; then
    if run --grab; then RC=0; else RC=$?; fi
    if [ "$RC" -eq 0 ]; then echo "S1 PASS"; else echo "S1 FAIL (exit=$RC want 0)"; fi
else
    echo "S1 SKIP (no seat headless — run in a VM/RDP backend)"
fi
$HT/stop.sh $ID
```

**Assert (S1, when a seat is present):** `RC == 0` — `grab_keyboard` delivers a
`keymap` (fd + size) and a `repeat_info` before any key event, proving the grab
is wired to the seat keyboard.

### S2 — registry audit (VM, with the full session)

Run `tests/gui/agent-protocol-audit.sh` in the VM; it now requires
`zwp_input_method_manager_v2` in the live registry alongside
`zwp_text_input_manager_v3`.

## Teardown

`stop.sh $ID` runs at the end of S1. If a case aborts earlier, tear down with
`$HT/stop.sh 24-im`.

## Pass criteria

S-static `exit 0`; S0 `RC==5` headless (or `0` with a seat); S1 PASS in a
seat-bearing backend (documented SKIP headless); S2 audit lists the global.
