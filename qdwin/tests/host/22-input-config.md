# tests/host/22-input-config.md — v28 live input config

Goal: prove the qdshell Mouse and Keyboard settings tabs apply LIVE through
the v28 `qdwin_shell_v1` requests instead of being persist-only:

- `set_pointer_config` applies the libinput pointer/touchpad policy (accel
  speed + profile, natural-scroll, tap-to-click, left-handed, middle-button
  emulation, disable-while-typing, scroll method) to every libinput device
  on the seats, and
- `set_key_repeat` sets the xkb key-repeat rate/delay and re-advertises it to
  every `wl_keyboard` via `wl_keyboard.repeat_info`.

Closes the `pointerConfig:false` / `xkbRepeat:false` persist-only gap in
qdshell `Services/Qdwin/CapabilityService.qml`.

## Probe

`qdwin-input-probe` binds `qdwin_shell_v1` as the shell, asserts the global
advertises `>= v28`, then pushes a valid snapshot followed by a deliberately
out-of-range / garbage snapshot. The compositor must accept both — clamping
and enum-normalising the bogus values server-side — without raising a
protocol error or dropping the connection.

## Cases

1. **accept + fail-safe** — `qdwin-input-probe`
   binds at v28, pushes `set_pointer_config` + `set_key_repeat` with sane
   values, then with out-of-range values (`accel_speed=999999`,
   `accel_profile=9999`, `scroll_method=4242`, `rate=0xffffff`, `delay=0`).
   Exit 0 (connection survived; no protocol error). The compositor log shows
   `qdwin: set_pointer_config …` and `qdwin: set_key_repeat …` lines.

2. **version gate** — against a compositor that advertised the global below
   v28 the probe exits 2 (it never reaches the requests). Not exercised on a
   current build (the global is v28) but documents the negative path.

3. **live-VM (PENDING)** — a real mouse/touchpad changes acceleration /
   natural-scroll / tap behaviour and a physical keyboard's repeat rate
   changes when qdshell pushes the settings. Requires a live VM session with
   physical (or virtio) input on the DRM/libinput backend; tracked as
   PENDING. On a headless host there are no libinput devices, so
   `set_pointer_config` applies to nothing (the no-op-safe path the probe
   asserts does not error).

## Status

`qdwin-input-probe` is built by `meson` (target `qdwin-input-probe`) and
compiles in CI, but — like `21-ext-workspace-names.md` — the accept /
fail-safe case is NOT wired into `meson test` because it needs a live qdwin
to bind against (and the probe must run as the shell-gated uid). Run it
against a headless qdwin (weston-launch or a nested qdwin) and assert exit 0
plus the two `weston_log` lines above.

The unit-level invariants (handler wiring, shell-bound gate, value clamping,
enum normalisation, libinput-backend-only device walk, per-device capability
guards, live `repeat_info` re-advertise, teardown reset, global advertised
`>= v28`) ARE covered by `meson test` via
`qdwin/test_input_config_policy.py` (test `input-config-policy`). The
live-VM half is PENDING.
