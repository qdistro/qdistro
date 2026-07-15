#!/usr/bin/env bats
# Live qdwin/qdshell session probes.
#
# This file once held the §6.5 per-view RDP regression suite: subscribe +
# qdistro-forward + sdl-freerdp RDP-broker tests driven over the legacy Python
# qdshell.py ctrl-socket, plus a few rdp-backend protocol probes (idle-inhibit,
# cursor-shape, fractional-scale, primary-selection) that needed a live
# sdl-freerdp peer to create a weston seat. That whole pipeline was archived
# under qdistro-old-plan/compositor/{qdshell,spike-6.5}/ when production qdshell
# moved to QML/Quickshell (no ctrl-socket, no RDP backend in the prod session).
# Round 6 Phase 3 (2026-05-18) `skip`-gated the dead probes with named reasons
# rather than deleting them; they have since been retired outright (git keeps
# the history), leaving only the two probes that drive the CURRENT stack:
#   - s6.7-xdg-activation — qdwin's xdg_activation_v1, via a pywayland probe
#     against a freshly-booted weston+qdwin (no ctrl-socket).
#   - launcher-foot-roundtrip — the P01 greeter→qdwin→qdshell boot path's
#     rocket-icon launcher round-trip, when a live GUI session is present.

load helpers

# launcher-foot-roundtrip skip reasons (kept as variables so grep finds the
# ledger from anywhere in this file).
SKIP_S103_NO_GUI="no qdwin GUI session on $VM_NAME (/run/user/1000/wayland-1 absent) — s103 needs a logged-in qdshell; boot the GUI VM first"
SKIP_S103_NO_INPUT="ydotool/foot not installed on $VM_NAME — s103 drives the rocket icon via synthetic input; see fresh-vm-bootstrap.sh uinput setup"

teardown_file() {
    reap_vm_drivers
}

@test "s6.7-xdg-activation: token issued, bogus activate silently ignored" {
    vm_run "bash /root/s7-xdg-activation.sh"
    assert_success
    assert_output_contains "PASS: token issued"
    assert_output_contains "PASS: §6.7 xdg-activation-v1 end-to-end"
}

# Full "real user" round-trip on top of the greeter boot path:
#   login → click the upper-left ROCKET icon (the qdshell launcher /
#   "start" button) → launch foot from the launcher → type `ls /` →
#   verify the listing is printed on screen (host-side libvirt display capture,
#   then guest-side tesseract OCR).
# Driver: tests/integration/vm/s103-launcher-foot-roundtrip.sh, staged
# over the host HTTP server (same pattern as app-launcher.bats).
@test "launcher-foot-roundtrip: rocket icon click launches foot, runs ls / and prints output" {
    # Needs a live qdwin GUI session...
    vm_run "test -S /run/user/1000/wayland-1"
    [ "$status" -eq 0 ] || skip "$SKIP_S103_NO_GUI"
    # ...and the synthetic-input + terminal tooling the driver drives.
    vm_run "command -v ydotool >/dev/null && command -v foot >/dev/null"
    [ "$status" -eq 0 ] || skip "$SKIP_S103_NO_INPUT"

    stage_vm_driver "s103-launcher-foot-roundtrip.sh"
    vm_run "curl -fsS -o /tmp/s103.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s103-launcher-foot-roundtrip.sh && chmod +x /tmp/s103.sh && bash /tmp/s103.sh"
    assert_success

    # Load-bearing PASS contract — one per step of the scenario.
    assert_output_contains "PASS: login complete — qdwin session is up"
    assert_output_contains "PASS: clicked rocket icon (upper-left) — launcher opened"
    assert_output_contains "PASS: foot terminal launched from the launcher"
    assert_output_contains "PASS: foot terminal is up and focused"
    assert_output_contains "PASS: typed 'ls /' into foot"
    assert_output_contains "READY: foot output is mapped for host display capture"

    # qdwin intentionally has no wlr-screencopy global, and Weston 14's capture
    # client is incompatible with this image's system-weston/vendored-libweston
    # boundary. Capture the actual virtual display from outside the guest. This
    # is the same honest pixel source a person sees through SPICE/RDP.
    local shot="$BATS_TEST_TMPDIR/s103-foot.png"
    run virsh screenshot "$VM_NAME" "$shot"
    assert_success
    [ -s "$shot" ] || fail_loud "virsh screenshot produced no image at $shot"

    # tesseract is baked in the VM. Serve the just-captured pixels over the
    # already-private driver HTTP server so OCR remains hermetic to the image.
    cp "$shot" "$(_qd_driver_stage_dir)/s103-foot.png"
    vm_run "command -v tesseract >/dev/null"
    require "tesseract not installed on VM (needed for launcher visual assertion)"
    vm_run "curl -fsS -o /tmp/s103-foot.png http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s103-foot.png && tesseract /tmp/s103-foot.png - 2>/dev/null"
    assert_success

    local ocr_text="$output" hits=0 d
    for d in usr etc bin var lib home tmp dev proc sbin boot run opt root; do
        grep -qiw "$d" <<<"$ocr_text" && hits=$((hits + 1))
    done
    if [ "$hits" -lt 2 ]; then
        echo "--- OCR output (matched only $hits root entries) ---" >&2
        echo "$ocr_text" >&2
        return 1
    fi

    echo "PASS: foot printed the root listing (OCR matched $hits root entries)"
    echo "PASS: launcher → foot → command round-trip end-to-end"
    vm_run "pkill -9 -x foot >/dev/null 2>&1 || true"
}
