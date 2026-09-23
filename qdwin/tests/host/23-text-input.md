# 23 — text-input-unstable-v3 foundation (manager advertised + focus enter/leave + inert contract)

**What**: pin qdwin's `zwp_text_input_v3` foundation (Bucket A / P1;
`qdwin_text_input_manager_get_text_input`, `qdwin_text_input_update_focus`,
the keyboard-focus enter/leave wiring, and the no-input-method "inert"
contract; `qdwin/qdwin.c`):

- `zwp_text_input_manager_v3` is advertised (open — every app needs text input)
  and binds;
- `get_text_input(seat)` succeeds and the **full request set** is accepted with
  no protocol error (enable, set_surrounding_text, set_text_change_cause,
  set_content_type, set_cursor_rectangle, commit, disable, commit, destroy);
- enter/leave is **focus-driven**: a `text_input` created while one of the
  client's surfaces holds the keyboard focus receives exactly one `enter`
  carrying that surface;
- the **foundation-only / inert contract**: with no input-method-v2 IME wired
  in, qdwin emits ZERO `preedit_string` / `commit_string` /
  `delete_surrounding_text` / `done` events, ever — even after enable+commit.
  A bound toolkit therefore sees focus but no composed text, which is the
  intended state until the (separately gated) input-method side lands.

**Why**: toolkits (GTK/Qt/Chromium) bind `zwp_text_input_manager_v3` directly
to know when a field can receive IME input; without the global they get no
text-input plane at all. The enter/leave focus wiring is what makes a field
"active"; the inert contract guarantees qdwin never fabricates composed text
without a real IME. This is the headless-reachable half of the IME item in
`todo/issues/qdwin/app-compat-protocol-gaps.md` (P1).

**Non-visual**: asserts on probe exit codes + the static-invariant meson test.

## Two-part coverage (read first)

`get_text_input` needs a `wl_seat`. The weston **headless** backend used by
this harness has **no input backend and advertises no `wl_seat`** (verified —
see `17-cursor-shape.md`, `19-hotkey-edges.md`, `20-rdp-focus-rebind.md`). So
the live enter/leave + request-lifecycle assertions are reachable only under a
backend WITH a seat (a VM, or weston's RDP/DRM/wayland backend). Coverage is
therefore split:

1. **Static invariants — fully headless, always run** (`meson test
   text-input-foundation`, `qdwin/test_text_input.py`): the manager is created
   open at v1, `get_text_input` and the keyboard `focus_signal` handler both
   call `qdwin_text_input_update_focus`, enter is client-scoped, and the inert
   contract holds (no `..._send_preedit_string` / `_send_commit_string` /
   `_send_delete_surrounding_text` / `_send_done` anywhere in the source).
2. **Live probe — seat-gated** (`qdwin-textinput-probe`): S0 runs headless and
   proves the manager is advertised + the no-seat gate (exit 5); S1/S2 are the
   real enter + inert assertions and PASS under a seat (VM), SKIP headless.

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-textinput-probe"
```

## Steps

### S-static — source invariants (always runs, no seat needed)

```bash
export PATH="$HOME/.local/bin:$PATH"
meson test -C "${QDWIN_BUILD:-build}" text-input-foundation
```

**Assert (S-static):** exit 0, `PASS: text-input-v3 foundation ...`. Fails if
the manager global drops/changes version, the focus wiring is removed, enter
stops being client-scoped, or qdwin starts emitting any IME/done event without
an input-method.

### S0 — manager advertised; functional path gated by seat presence (always runs)

```bash
ID=23-ti
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID)
run() { XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" "$@"; }

if run --lifecycle; then RC=0; else RC=$?; fi
SEAT_PRESENT=0
[ "$RC" -ne 5 ] && SEAT_PRESENT=1
if [ "$RC" -eq 5 ]; then
    echo "S0 PASS (headless: zwp_text_input_manager_v3 advertised, no seat — functional path is VM-only, documented)"
elif [ "$RC" -eq 0 ]; then
    echo "S0 PASS (seat present: manager advertised AND lifecycle inert contract held)"
else
    echo "S0 FAIL (exit=$RC; want 5 headless or 0 with a seat)"
fi
```

**Assert (S0):**
- `RC == 5` headless (manager advertised, `get_text_input` blocked on no seat
  — the documented headless outcome), or `RC == 0` under a seat (the full
  request lifecycle ran and produced no enter and no IME/done events).
- Any other exit is a real failure.

### S1 — focus enter on the focused surface (seat-gated; PASS in VM, SKIP headless)

```bash
if [ "$SEAT_PRESENT" = 1 ]; then
    if run --focus; then RC=0; else RC=$?; fi
    if [ "$RC" -eq 0 ]; then echo "S1 PASS"; else echo "S1 FAIL (exit=$RC want 0)"; fi
else
    echo "S1 SKIP (no seat headless — run in a VM/RDP backend)"
fi
$HT/stop.sh $ID
```

**Assert (S1, when a seat is present):**
- `RC == 0` — a `text_input` created after the probe's own xdg_toplevel is
  mapped+autofocused receives **exactly one** `enter` carrying that surface,
  and STILL zero `preedit_string` / `commit_string` / `done` (enter is
  focus-driven, not IME-driven).

### S2 — listener/list cleanup churn (seat-gated; PASS in VM, SKIP headless)

```bash
if [ "$SEAT_PRESENT" = 1 ]; then
    if run --stress; then RC=0; else RC=$?; fi
    if [ "$RC" -eq 0 ]; then echo "S2 PASS"; else echo "S2 FAIL (exit=$RC want 0)"; fi
else
    echo "S2 SKIP (no seat headless — run in a VM/RDP backend)"
fi
$HT/stop.sh $ID
```

**Assert (S2, when a seat is present):**
- `RC == 0` — 5 rounds of map+autofocus → `get_text_input` → destroy the
  **focused surface while the text_input is alive** → destroy the text_input,
  with **no protocol error / compositor disconnect** and no IME events. This
  drives qdwin's entered-surface destroy listener (clear-without-leave) and the
  list cleanup under real Wayland dispatch — a dangling listener or UAF shows
  up as a disconnect. (Note: this exercises per-object teardown during a live
  session; it does NOT prove compositor-shutdown ordering, which the static
  `text-input-foundation` drain invariants cover.)

## Teardown

`stop.sh $ID` runs at the end of S2. If a case aborts earlier, tear down with
`$HT/stop.sh 23-ti`.

## Pass criteria

S-static `exit 0`; S0 `RC==5` headless (or `0` with a seat); S1 + S2 PASS in a
seat-bearing backend (documented SKIP headless).
