# Settings → Mouse & Touchpad

What must be visible when this tab is open:

- Tab header / first section text: "Devices" (i18n key `panels.mouse.section-devices`).
- A "Refresh" button on the Devices section header.
- One row per detected pointer device (mouse / touchpad / trackpoint / graphics
  tablet) with a type label, OR the "No pointer devices detected" empty state
  (`panels.mouse.no-devices-label`) when none can be enumerated. Graphics
  tablets / Wacom digitizers show the `panels.mouse.device-type-tablet` label.
- A "Pointer" section with an acceleration-profile combo and a pointer-speed
  slider, plus a left-handed-mode toggle.
- A "Buttons & Click" section (`panels.mouse.section-buttons`) with a click-method
  combo (button areas vs clickfinger) and a middle-click-emulation toggle.
- A "Scrolling" section with natural-scroll and horizontal-scroll toggles and a
  scroll-method combo.
- A "Touchpad" section with tap-to-click and disable-while-typing toggles.
- A "Double-click & Drag" section with double-click time/distance and
  drag-threshold spin boxes.
- A "Per-device overrides" section (`panels.mouse.section-per-device`) with one
  card per enumerated device: a "Device enabled" toggle (per-device
  enable/disable) and, for non-tablet devices, a "Customize this device" toggle
  that reveals per-device accel-profile / speed / natural-scroll / left-handed
  controls plus a "Reset to global" button.
- A "Tablet mapping" section (`panels.mouse.section-tablet`): when a tablet is
  detected, a map-to-output combo (all outputs + each connected screen), an
  aspect-ratio combo (keep/stretch), and area left/top/width/height percentage
  spin boxes; otherwise the `panels.mouse.no-tablet-label` empty state.
- A cursor hint pointing to the Appearance tab (`panels.mouse.cursor-hint`).
- The standard left-side Settings tab strip is visible.

Notes:
- qdshell is qdwin-only. qdwin_shell_v1 v28 can live-apply only the global
  pointer fields listed in `panels.mouse.live-fields-note`; older backends show
  the persist-only banner (`panels.mouse.backend-persist-only`) and save those
  choices until support is available. There is NO live dispatch to any foreign
  compositor (no sway/hyprland/wlr).
- Click method, horizontal-scroll toggle, per-device overrides, device
  enable/disable, double-click/drag thresholds, and tablet mapping are saved
  only even when the global pointer backend is live.
- Per-device overrides are keyed by the (untrusted) enumerated device id, used
  only as an opaque map key — never shell-interpolated. Device names render as
  plain text.
- Double-click time/distance and drag threshold are persisted UI-interaction
  preferences and are not applied through libinput.
