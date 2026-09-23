# 26 — lock curtain occludes the desktop under a transparent locker

**What**: drive `qdwin-locker-probe --transparent-lock` against the headless
compositor to check the compositor-side lock-occlusion invariant
(`qdwin_install_lock_curtain`, `qdwin/qdwin.c`): a broken/hostile locker that
attaches a fully **transparent** lock surface and calls `set_locked(1)` must
still not reveal the desktop. qdwin keeps an opaque black `weston_curtain` at
the bottom of `lock_layer` while locked, so the worst case is a black screen,
never a desktop leak.

**Why**: the lock surface is the only thing composited while locked
(`qdwin_hide_non_lock_layers` unsets every other layer, including
`background_layer` and its own black curtain). If the locker's buffer is
transparent — as qdlocker's Qt surface was under software GL (it committed an
empty buffer, and the damage-tracked renderer left stale desktop pixels on
screen) — the desktop leaks straight through the lock screen. That is a
security regression: the lock screen MUST fully occlude desktop content. The
client bug is fixed in qdlocker (force the software Qt Quick backend), but the
compositor must not *trust* the locker to paint opaque pixels; this test
enforces that boundary in qdwin independently of any client.

**Non-visual**: asserts on the `weston-debug scene-graph` dump (view opacity,
geometry, stacking, and buffer colour), not a screenshot — deterministic and
free of the `weston-screenshooter` dependency.

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-locker-probe"
WINDOW="$QDWIN_INSTALL/bin/qdistro-test-window"
ID=26-lock-curtain-occludes
```

## Steps

### S1 — a transparent locker must not expose the magenta desktop

Bring up qdwin + qdshell, put a full-screen magenta (`#ff00ff`) desktop window
on the normal layer, then engage a locker that attaches a transparent
1920×1080 lock surface and locks. Dump the scene graph while locked and assert
the lock layer carries an opaque black full-output curtain beneath the
transparent lock surface.

```bash
$HT/stop.sh $ID >/dev/null 2>&1 || true
$HT/start.sh $ID --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID)
run() { XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$@"; }

# Magenta desktop sentinel (fills the 1024x640 headless output).
run "$WINDOW" --title sentinel --color 0xffff00ff --width 1024 --height 640 \
    >/dev/null 2>&1 &
WIN=$!
sleep 1

# Broken locker: transparent lock surface + set_locked(1); idles until killed.
run "$PROBE" --transparent-lock > "$(ht_dir $ID)/probe.out" 2>&1 &
PROBE_PID=$!
for i in $(seq 1 50); do
    grep -q "transparent lock engaged" "$(ht_dir $ID)/probe.out" 2>/dev/null && break
    sleep 0.1
done
sleep 0.5

SG="$(ht_dir $ID)/scene-graph.txt"
run timeout 2 weston-debug scene-graph > "$SG" 2>&1

# Assert BEFORE stop.sh (stop.sh rm -rf's the test dir).
if python3 "$HT/lock-curtain-assert.py" "$SG"; then RC=0; else RC=1; fi

kill "$PROBE_PID" "$WIN" 2>/dev/null || true
$HT/stop.sh $ID
if [ "$RC" -eq 0 ]; then echo "S1 PASS"; else echo "S1 FAIL"; fi
```

**Expected**: `S1 PASS`. The scene graph's lock layer contains two views — the
probe's transparent lock surface (`[not opaque]`, ARGB8888) on top, and a
server-created (PID 0) `[fully opaque]` solid-colour `[R 0, G 0, B 0, A 1]`
view covering `(0,0)→(1024,640)` beneath it. Every desktop layer is
`[no views]`. Without the curtain (revert `qdwin_install_lock_curtain`) the
lock layer holds only the transparent surface and the assertion FAILs — which
is exactly the leak this guards against.

## Notes

- The probe's `--transparent-lock` mode uses the legacy
  `qdwin_locker_v1.attach_lock_surface` path (a real lock cycle with input
  injection is a VM follow-up; see `tests/gui/`). The curtain is installed in
  both `set_locked` handlers, so this exercises the same invariant the real
  toplevel-promotion path relies on.
- `start.sh` defaults to `QDWIN_ALLOWED_LOCKER_ANY=1` (uid-only locker policy),
  so the native probe is accepted as the locker.
