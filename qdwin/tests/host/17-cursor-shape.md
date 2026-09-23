# 17 — wp_cursor_shape_v1 set_shape validation + edges (VM-only device path)

**What**: drive `qdwin-cursor-probe` against qdwin's `wp_cursor_shape_v1`
implementation (`qdwin_cursor_shape_manager_get_pointer`,
`qdwin_cursor_shape_device_set_shape`; `qdwin/qdwin.c`):

- an out-of-range shape (0 or > `shape_all_resize`) is REFUSED with the
  dedicated `invalid_shape` protocol error (fail-closed validation);
- a valid shape is accepted and logged (theme HIT or theme MISS — the miss
  path must not error);
- a burst of repeated `set_shape` calls is all accepted (no leak/error);
- `set_shape` on a SECOND device (cursor after a focus change) still works.

**Why**: `set_shape` is the request a client uses to pick its pointer
sprite; a compositor that didn't validate the shape enum could index its
`cursor_images[]` table out of bounds (a read past the array), and one that
errored on a theme miss would break every client on a host with no cursor
theme installed. The `invalid_shape` reject is the fail-closed seam. This is
the cursor-shape item in `todo/codex-testing/under-tested-areas.md` §3.

**Non-visual**: asserts on probe exit codes and weston-log evidence.

## Headless limitation — this is a VM-only DEVICE path (read first)

The cursor-shape **device** is obtained via
`wp_cursor_shape_manager_v1.get_pointer(id, wl_pointer)`, and a `wl_pointer`
exists only on a `wl_seat`. The weston **headless** backend used by this
harness has **no input backend and advertises no `wl_seat`** (verified:
`wp_cursor_shape_manager_v1` IS in the registry, but `wl_seat` is not). With
no seat there is no `wl_pointer`, so the device — and therefore every
`set_shape` call, including the `invalid_shape` validation — is **unreachable
headless**.

The probe encodes this precisely:
- it exits **5** when no `wl_seat` is advertised (the expected headless
  outcome), distinct from a real failure (1) or setup error (2);
- under a backend WITH a seat (a VM with a real or virtual input device, or
  weston's DRM/wayland backend), the same probe binds the seat's pointer,
  builds the device, and runs the real assertions below.

So S0 is the runnable headless check (manager advertised + the no-seat
blocker proven); S1–S4 are the real assertions, **gated on a seat being
present** — they PASS in a VM and SKIP (documented) headless. The companion
VM follow-up belongs next to the other seat-dependent suites
(`tests/gui/`, multi-uid).

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-cursor-probe"
ID=17-cursor
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)
run() { XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" "$@"; }
```

## Steps

### S0 — manager advertised; device gated by seat presence (always runs)

```bash
# The probe exits 5 headless (no seat) or 0 on the happy device path.
if run --valid; then RC=0; else RC=$?; fi
SEAT_PRESENT=0
[ "$RC" -ne 5 ] && SEAT_PRESENT=1
if [ "$RC" -eq 5 ]; then
    echo "S0 PASS (headless: wp_cursor_shape_manager_v1 advertised, no seat — device path is VM-only, documented)"
elif [ "$RC" -eq 0 ]; then
    echo "S0 PASS (seat present: device built, valid set_shape accepted)"
else
    echo "S0 FAIL (exit=$RC want 5=no-seat headless OR 0=device ok)"
fi
```

**Assert (S0):** `RC == 5` headless (the documented no-seat blocker, proving
the manager is advertised and the only thing missing is a seat) OR `RC == 0`
in a VM (device built, valid shape accepted). Any other code is a FAIL.

### S1 — invalid shape (0) is REFUSED with invalid_shape  *(seat required)*

```bash
if [ "$SEAT_PRESENT" -eq 1 ]; then
    if run --invalid-low; then RC=0; else RC=$?; fi
    if [ "$RC" -eq 3 ]; then echo "S1 PASS"; else echo "S1 FAIL (exit=$RC want 3=invalid_shape)"; fi
else
    echo "S1 SKIP (no seat — VM-only; documented headless limitation)"
fi
```

**Assert (S1, seat present):** `RC == 3` — `set_shape(0)` raised exactly
`WP_CURSOR_SHAPE_DEVICE_V1_ERROR_INVALID_SHAPE` (=1) on the device (the probe
checks the code AND interface).

### S2 — invalid shape (max+1) is REFUSED with invalid_shape  *(seat required)*

```bash
if [ "$SEAT_PRESENT" -eq 1 ]; then
    if run --invalid-high; then RC=0; else RC=$?; fi
    if [ "$RC" -eq 3 ]; then echo "S2 PASS"; else echo "S2 FAIL (exit=$RC want 3)"; fi
else
    echo "S2 SKIP (no seat — VM-only)"
fi
```

**Assert (S2, seat present):** `RC == 3` — `set_shape(shape_all_resize + 1)`
raised `invalid_shape`.

### S3 — repeated shape changes are all accepted  *(seat required)*

```bash
if [ "$SEAT_PRESENT" -eq 1 ]; then
    if run --burst; then RC=0; else RC=$?; fi
    SC=$(grep -c "qdwin: cursor-shape set_shape=" "$WLOG" || true)
    if [ "$RC" -eq 0 ] && [ "$SC" -ge 6 ]; then echo "S3 PASS"; else echo "S3 FAIL (exit=$RC want 0; set_shape-log=$SC want >=6)"; fi
else
    echo "S3 SKIP (no seat — VM-only)"
fi
```

**Assert (S3, seat present):** `RC == 0` AND the weston log shows >=6
`qdwin: cursor-shape set_shape=…` lines (one per shape in the burst) — proves
each change reached the handler and was accepted (theme hit or miss).

### S4 — set_shape after a focus change (second device) works  *(seat required)*

```bash
if [ "$SEAT_PRESENT" -eq 1 ]; then
    if run --refocus; then RC=0; else RC=$?; fi
    if [ "$RC" -eq 0 ]; then echo "S4 PASS"; else echo "S4 FAIL (exit=$RC want 0)"; fi
else
    echo "S4 SKIP (no seat — VM-only)"
fi
```

**Assert (S4, seat present):** `RC == 0` — a second `get_pointer` device and a
`set_shape` on it round-trip cleanly (cursor-after-focus-change path).

## Teardown

```bash
$HT/stop.sh $ID
```

## Pass criteria

- Headless (this harness): S0 PASS with `RC==5` (manager advertised, no seat
  — device path documented VM-only); S1–S4 SKIP.
- VM (seat present): S0 `RC==0`, S1/S2 `RC==3` (invalid_shape), S3 `RC==0` +
  >=6 set_shape log lines, S4 `RC==0`.
