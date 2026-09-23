#!/usr/bin/env python3
"""test_input_config_policy.py — source-level invariants for the v28 live
input-config feature (qdwin_shell_v1.set_pointer_config / set_key_repeat).

Asserts the C source:

  * implements both request handlers and wires them into the
    qdwin_shell_v1 interface vtable,
  * gates each request behind the shell-bound check (only the shell may
    reconfigure input),
  * clamps out-of-range values fail-safe (accel speed, repeat rate/delay),
  * normalises the accel-profile / scroll-method enums with a defined
    fallback (never trusts an arbitrary uint),
  * only walks libinput device lists under the libinput/DRM backend
    (the udev_seat reinterpret is unsafe on headless/RDP/nested seats),
  * guards every per-device libinput setter with its capability query so a
    mixed mouse+touchpad seat never errors on an unsupported field,
  * re-advertises wl_keyboard.repeat_info live (not just at bind time),
  * resets the live policy / restores the default key-repeat when the shell
    binding is torn down,
  * advertises the global at a version >= 28 so the new requests are
    actually reachable.

Pure source check, host-independent, so it lives in the unit test() set
(see meson.build). The live-VM half — a real mouse/touchpad / physical
keyboard reacting to the applied settings — is tracked PENDING in
tests/host/22-input-config.md.
"""
import re
import sys
import pathlib


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    src = pathlib.Path(sys.argv[1]).read_text()

    # ---- set_pointer_config handler ----
    m = re.search(
        r"qdwin_handle_set_pointer_config\s*\(\s*struct wl_client[^)]*"
        r"int32_t\s+accel_speed[^)]*scroll_method\s*\)",
        src,
        re.S,
    )
    if not m:
        fail("set_pointer_config handler signature not found")
    pc_body = src[m.start():m.start() + 4000]

    if "qdwin_shell_require_bound" not in pc_body:
        fail("set_pointer_config does not gate on shell-bound check")
    # accel speed clamped to the documented [-1000, 1000] range.
    if "QDWIN_ACCEL_SPEED_MIN" not in pc_body or \
       "QDWIN_ACCEL_SPEED_MAX" not in pc_body:
        fail("set_pointer_config does not clamp accel_speed")
    # scroll_method has a switch with a default fallback (never trusts uint).
    if "default:" not in pc_body or "QDWIN_SCROLL_TWO_FINGER" not in pc_body:
        fail("set_pointer_config does not normalise scroll_method enum")
    # accel_profile normalised (flat vs adaptive fallback).
    if "QDWIN_ACCEL_FLAT" not in pc_body or "QDWIN_ACCEL_ADAPTIVE" not in pc_body:
        fail("set_pointer_config does not normalise accel_profile enum")

    # ---- set_key_repeat handler ----
    m2 = re.search(
        r"qdwin_handle_set_key_repeat\s*\(\s*struct wl_client[^)]*"
        r"uint32_t\s+rate\s*,\s*uint32_t\s+delay\s*\)",
        src,
        re.S,
    )
    if not m2:
        fail("set_key_repeat handler signature not found")
    kr_body = src[m2.start():m2.start() + 2500]
    if "qdwin_shell_require_bound" not in kr_body:
        fail("set_key_repeat does not gate on shell-bound check")
    if "QDWIN_KB_RATE_MAX" not in kr_body or \
       "QDWIN_KB_DELAY_MIN" not in kr_body or \
       "QDWIN_KB_DELAY_MAX" not in kr_body:
        fail("set_key_repeat does not clamp rate/delay")
    if "kb_repeat_rate" not in kr_body or "kb_repeat_delay" not in kr_body:
        fail("set_key_repeat does not update the compositor repeat rate/delay")
    if "qdwin_resend_repeat_info" not in kr_body:
        fail("set_key_repeat does not re-advertise repeat_info live")

    # The clamp macros are actually defined.
    for macro in ("QDWIN_ACCEL_SPEED_MIN", "QDWIN_ACCEL_SPEED_MAX",
                  "QDWIN_KB_RATE_MAX", "QDWIN_KB_DELAY_MIN",
                  "QDWIN_KB_DELAY_MAX"):
        if not re.search(r"#define\s+" + macro + r"\b", src):
            fail(f"{macro} not defined")

    # ---- vtable wiring ----
    if not re.search(r"\.set_pointer_config\s*=\s*qdwin_handle_set_pointer_config",
                     src):
        fail("set_pointer_config not wired into qdwin_shell_impl vtable")
    if not re.search(r"\.set_key_repeat\s*=\s*qdwin_handle_set_key_repeat", src):
        fail("set_key_repeat not wired into qdwin_shell_impl vtable")

    # ---- live re-advertise of repeat_info honours the wl_keyboard since gate
    rs = re.search(r"qdwin_resend_repeat_info\s*\(struct qdwin[^{]*\{", src)
    if not rs:
        fail("qdwin_resend_repeat_info not defined")
    rs_body = src[rs.start():rs.start() + 1500]
    if "WL_KEYBOARD_REPEAT_INFO_SINCE_VERSION" not in rs_body:
        fail("repeat_info re-advertise ignores the wl_keyboard since gate")
    if "wl_keyboard_send_repeat_info" not in rs_body:
        fail("repeat_info re-advertise does not call wl_keyboard_send_repeat_info")

    # ---- pointer device walk only under the libinput backend ----
    ap = re.search(r"qdwin_pointer_config_walk\s*\(struct qdwin[^{]*\{",
                   src)
    if not ap:
        fail("qdwin_pointer_config_walk not defined")
    ap_body = src[ap.start():ap.start() + 1500]
    if "libinput_backend" not in ap_body:
        fail("device walk does not gate the udev_seat reinterpret on the "
             "libinput backend")
    # Synthetic (non-libinput) seats are skipped even under the backend: the
    # walk must exclude both the RDP seats and ALL qdwin- synthetic seats
    # (per-stream + nested), not just one prefix.
    if '"rdp-"' not in ap_body:
        fail("device walk does not skip RDP synthetic seats")
    if '"qdwin-"' not in ap_body:
        fail("device walk does not skip qdwin- synthetic seats "
             "(per-stream / nested)")
    # apply_all / reset_all both exist (reset reverts devices on unbind).
    if "qdwin_pointer_config_apply_all" not in src:
        fail("qdwin_pointer_config_apply_all not defined")
    if "qdwin_pointer_config_reset_all" not in src or \
       "qdwin_pointer_config_reset_device" not in src:
        fail("pointer reset-to-defaults (reset_all / reset_device) not defined")

    # libinput_backend is set from the backend type (not assumed true).
    if "WESTON_BACKEND_DRM" not in src or "weston_get_backend_type" not in src:
        fail("libinput_backend not derived from weston_get_backend_type")

    # ---- per-device capability guards ----
    dev = re.search(r"qdwin_pointer_config_apply_device\s*\([^{]*\{", src)
    if not dev:
        fail("qdwin_pointer_config_apply_device not defined")
    dev_body = src[dev.start():dev.start() + 3000]
    guards = [
        "libinput_device_config_accel_is_available",
        "libinput_device_config_scroll_has_natural_scroll",
        "libinput_device_config_tap_get_finger_count",
        "libinput_device_config_left_handed_is_available",
        "libinput_device_config_middle_emulation_is_available",
        "libinput_device_config_dwt_is_available",
        "libinput_device_config_scroll_get_methods",
    ]
    for g in guards:
        if g not in dev_body:
            fail(f"apply_device missing capability guard: {g}")

    # ---- teardown resets the live policy / restores default key repeat ----
    td = re.search(r"qdwin_shell_resource_destroy\s*\([^{]*\{", src)
    if not td:
        fail("qdwin_shell_resource_destroy not found")
    td_body = src[td.start():td.start() + 2500]
    if "pointer_config.valid = 0" not in td_body:
        fail("shell teardown does not drop the live pointer config")
    if "qdwin_pointer_config_reset_all" not in td_body:
        fail("shell teardown does not revert libinput devices to defaults")
    if "kb_repeat_overridden" not in td_body:
        fail("shell teardown does not restore the default key-repeat")

    # ---- the global advertises a version that actually reaches v28 ----
    g = re.search(
        r"wl_global_create\(\s*ec->wl_display\s*,\s*"
        r"&qdwin_shell_v1_interface\s*,\s*(\d+)\s*,",
        src,
    )
    if not g:
        fail("qdwin_shell_v1 wl_global_create not found")
    if int(g.group(1)) < 28:
        fail(f"qdwin_shell_v1 global advertised at v{g.group(1)} < 28 — the "
             "v28 requests would be unreachable")

    print("ok: input-config (v28) source invariants hold")


if __name__ == "__main__":
    main()
