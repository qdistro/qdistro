# 01 — Noctalia bar + wallpaper visible

**Acceptance criterion:** with Noctalia running on qdwin, a screenshot
of the VM shows (a) the Noctalia bar at the top edge, (b) the
wallpaper occupying the rest. No protocol errors in the weston
journal during a 10-second observation window.

This is the smoke test for "did the layer-shell port survive a
visual run?" — equivalent of `qdwin/01-open-terminal.md` for the
qdshell.py world but for the new shell.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
source ${QDISTRO_REPO}/tests/integration/qdwin-noctalia/noctalia-helpers.sh
qdwin_set_vm "${VMNAME:-noctalia-vis-260503-1021}"

pgrep -f "[h]ttp.server 8765" >/dev/null || (
 cd ${QDISTRO_REPO}/compositor && \
 python3 -m http.server 8765 --bind 127.0.0.1 >/tmp/qdistro-http.log 2>&1 &
)
sleep 1

noct_session_healthy || { echo "FAIL: qdshell.service not active"; exit 1; }
```

## Steps

### Step 1 — wake screen + capture baseline

```bash
noct_screenshot_awake /tmp/01-step1-baseline.png
```

**Assert (1.1):** the screenshot resolution is 1280×800 (the fixed GUI-CI
output geometry).
**Assert (1.2):** the top 31 px contain visible non-black pixels —
this is the Noctalia bar (`qdshell-bar-content` layer surface).
A simple check: pick row y=15, count distinct colors > 5.
**Assert (1.3):** the bottom 80% of the image (y >= 160) contains
the Noctalia owl/moon wallpaper or a uniform configured background.

### Step 2 — verify current-boot layer-shell journal is clean

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
 "runuser -l admin -c \"journalctl --user -u qdwin-compositor.service --boot --no-pager\"" \
 > "${QCI_SCENARIO_TMPDIR:-/tmp}/01-weston.log"
```

The disposable worker can be healthy for several minutes before a visual
runner reaches this step. Use the current boot rather than a wall-clock window
so the compositor-start mapping evidence cannot age out while the test is
running. A clean current-boot journal is also stronger than checking only the
last two minutes for protocol errors.

**Assert (2.1):** at least one `qdwin: layer-shell mapped
ns=qdshell-bar-content-` line is present.
**Assert (2.2):** at least one `qdwin: layer-shell mapped
ns=qdshell-wallpaper-` line is present.
**Assert (2.3):** zero lines matching `error <N>:` from any
`zwlr_layer_*` interface in the captured log.

### Step 3 — observe stability

```bash
sleep 10
noct_screenshot_awake /tmp/01-step3-after-10s.png
```

**Assert (3.1):** the bar is still in the top 31 px (compare
distinct-color count of the top strip in the new screenshot — must
be approximately the same as step 1).
**Assert (3.2):** qdshell.service still active —
`noct_session_healthy`.

## Cleanup

None — leave Noctalia running for subsequent scenarios.

## Pass criteria

All asserts in steps 1-3 pass.

## Known failure modes

1. **DPMS-off (black screenshot)** — weston blanks at 5min idle.
 `noct_screenshot_awake` includes a cursor wake; if the screenshot
 is still black after wake, weston may have hung. Restart with
 `noct_restart`.

2. **Privacy modal still up on first run** — Noctalia 4.x shows a
 "Privacy Update" consent modal on first launch. If the assert
 in 1.3 sees the modal instead of the wallpaper, run scenario
 02 first to dismiss it.

3. **`weston: fatal: unhandled option`** — older `weston.ini`
 versions with `--tty=N` flag don't work with libweston 14.
 Symptom: `qdwin-compositor.service` won't start. Fix:
 `sed -i 's/ --tty=2//' /home/admin/.config/systemd/user/qdwin-compositor.service`.

4. **MESA-LOADER GLES segfault** — if `weston.ini` doesn't include
 `renderer=pixman`, libgallium tries virtio-gpu accel3d (which
 isn't available in our VMs) and segfaults. Force pixman.
