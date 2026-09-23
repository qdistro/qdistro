# 18 — wp_fractional_scale_v1 preferred-scale delivery + dedup

**What**: drive `qdwin-fractional-probe` against the headless compositor to
pin qdwin's `wp_fractional_scale_v1` path
(`qdwin_fractional_scale_manager_get`, `qdwin_fractional_scale_push`,
`qdwin_compute_preferred_scale_for_surface`; `qdwin/qdwin.c`):

- `get_fractional_scale(surface)` delivers exactly one initial
  `preferred_scale` whose value equals the compositor's computed scale
  (120 = 1.0× headless default; or a forced non-integer via
  `QDWIN_FRACTIONAL_SCALE`, e.g. 180 = 1.5×);
- a surface commit that does not change the scale sends NO further
  `preferred_scale` (the `last_sent_scale` dedup holds);
- every tracked fractional-scale object receives the broadcast (multi-object).

**Why**: a scale-aware client renders its buffers at the advertised
fraction; a missing initial event leaves it at 1.0× (blurry on HiDPI), and a
duplicate/unchanged-value spam wastes a full re-render each commit. The
preferred-scale rebroadcast + dedup is the contract scale-aware Qt/GTK
clients depend on. This is the headless half of the fractional-scale item in
`todo/codex-testing/under-tested-areas.md` §3.

**Non-visual**: asserts on probe exit codes — fully scriptable, no seat or
GUI needed (the fractional-scale object binds a `wl_compositor` surface, not
a `wl_pointer`).

**Headless scope notes (read before relying on this)**:
- The headless backend exposes a single 1.0× output, so the natural computed
  scale is 120. A genuinely non-integer scale (1.5× → 180) is forced through
  the documented `QDWIN_FRACTIONAL_SCALE` env knob (qdwin clamps to 30..960).
  This exercises the non-integer send path; it does NOT exercise a real
  multi-scale output change at runtime.
- Multi-OUTPUT rebroadcast on a live `output_scale` change, and
  output-removal re-evaluation, need a backend that can add/remove/rescale
  outputs at runtime (the wlr-output-management apply path, or a DRM/VM
  backend). The `--dedup` and `--multi` cases here pin the per-object
  broadcast + dedup machinery that a real output change would drive; the
  end-to-end "change an output's scale and observe a rebroadcast on every
  object" is a VM/output-management follow-up (see tests/host/12-output-
  protocol.md for the output-management apply surface).

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-fractional-probe"
```

## Steps

### S1 — initial preferred_scale is delivered with the 1.0× value (120)

```bash
ID=18-frac-initial
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID)

# `run` may exit nonzero; capture set-e-safe (lib.sh has `set -e`).
if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --initial --expect=120; then RC=0; else RC=$?; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ]; then echo "S1 PASS"; else echo "S1 FAIL (exit=$RC want 0)"; fi
```

**Assert (S1):**
- `RC == 0` — `get_fractional_scale` delivered EXACTLY ONE `preferred_scale`
  event whose value is 120 (the headless 1.0× scale). The probe fails on a
  missing event, a duplicate, or a wrong value.

### S2 — a forced non-integer scale (1.5× → 180) is delivered verbatim

```bash
ID=18-frac-nonint
QDWIN_FRACTIONAL_SCALE=180 $HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --initial --expect=180; then RC=0; else RC=$?; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ]; then echo "S2 PASS"; else echo "S2 FAIL (exit=$RC want 0)"; fi
```

**Assert (S2):**
- `RC == 0` — with `QDWIN_FRACTIONAL_SCALE=180` the initial `preferred_scale`
  carries exactly 180 (1.5×), proving qdwin forwards a genuine non-integer
  scale unrounded. (Also confirms the env knob is honoured, so S1's 120 was
  the real computed default and not a hard-coded constant.)

### S3 — unchanged commits do NOT re-send preferred_scale (dedup)

```bash
ID=18-frac-dedup
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --dedup --expect=120; then RC=0; else RC=$?; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ]; then echo "S3 PASS"; else echo "S3 FAIL (exit=$RC want 0)"; fi
```

**Assert (S3):**
- `RC == 0` — after the initial event, two surface commits with no scale
  change produced ZERO additional `preferred_scale` events. The probe fails
  if any duplicate arrives, proving `qdwin_fractional_scale_push`'s
  `last_sent_scale` dedup is real (not a re-send on every commit).

### S4 — the broadcast reaches every tracked object (multi)

```bash
ID=18-frac-multi
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE" --multi --expect=120; then RC=0; else RC=$?; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ]; then echo "S4 PASS"; else echo "S4 FAIL (exit=$RC want 0)"; fi
```

**Assert (S4):**
- `RC == 0` — two fractional-scale objects on two surfaces each received the
  initial `preferred_scale=120`. Confirms the per-object delivery the
  broadcast loop (`qdwin_fractional_scale_broadcast`) relies on reaches
  every object, not just the first.

## Teardown

`stop.sh` runs inline per case. If a case aborts before its `stop.sh`, tear
each down individually, e.g. `$HT/stop.sh 18-frac-initial`.

## Pass criteria

All four asserts hold: S1 `RC==0` (initial 120), S2 `RC==0` (non-integer
180), S3 `RC==0` (dedup), S4 `RC==0` (multi-object broadcast).
