#!/usr/bin/env bats
# M1 — qdwin layer-popup edge clamp (security backstop).
#
# A tier-0/1 xdg_popup whose positioner places it far outside the parent
# (the spoofing surface a malicious client would use to paint over other
# windows / off-screen) MUST be pulled back inside the output. The clamp is
# the pure kernel qdwin_xdg_constrain_geometry() in the VENDORED, patched
# libweston-14 (libweston-vendored/.../desktop/xdg-shell.c); stock
# libweston-14 has no such constraint and echoes the requested offset
# straight through. The kernel's in-bounds invariant is pinned host-side by
# qdwin/tests/unit/test-qdwin-logic.c; THIS lane proves the clamp is actually
# wired and active in a live qdwin session — i.e. the compositor loaded the
# vendored tree (via noctalia-session.service's LD_LIBRARY_PATH/
# WESTON_MODULE_MAP, written by install-qdwin-session-for-vm.sh) and the real
# protocol path constrains a hostile popup.
#
# Driver: qdwin-popup-probe (a direct xdg_shell client, shipped by the qdwin
# build) prints `POPUP_GEOM <x> <y> <w> <h>` — the geometry the compositor
# returns in xdg_popup.configure, in the parent window-geometry frame.

load helpers

# The qdwin session pins its output to 1920x1080 (the weston.ini written by
# install-qdwin-session-for-vm.sh). The probe's parent toplevel is mapped at
# the output origin, so POPUP_GEOM x/y are output coordinates and the in-bounds
# invariant is `x+w <= OUTPUT_W && y+h <= OUTPUT_H`. If the session resolution
# changes, update these.
OUTPUT_W=1920
OUTPUT_H=1080

setup_file() {
    # A live qdwin compositor session on wayland-1 is required (the base
    # fresh-vm-bootstrap brings it up; the qdwin gui profile keeps it).
    vm_run "test -S /run/user/1000/wayland-1"
    require "no qdwin compositor on wayland-1 — this lane needs a live qdwin session"

    # The clamp lives in the VENDORED libweston. Post-bake (cairo-devel in
    # install-deps.sh + the fixed install-vendored-libweston.sh) this is a
    # SHIPPED deliverable, not optional — so fail LOUD (not skip) if the
    # compositor is running stock libweston-14: that means the vendored build/
    # staging regressed and the clamp is silently absent.
    vm_run "pmap \$(pgrep -x weston | head -1) 2>/dev/null | grep -q '/usr/libexec/qdistro/qdwin-libweston/.*/libweston-14\.so'"
    require "compositor is NOT running the vendored libweston (popup clamp absent) — vendored build/staging regressed; check install-vendored-libweston.sh + cairo-devel in install-deps.sh"

    vm_run "test -x /usr/bin/qdwin-popup-probe"
    require "qdwin-popup-probe not installed (qdwin build/install regressed)"

    # The probe connects as admin (the compositor's allowed uid).
    vm_run "chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true"
}

# _probe <offset-x> <offset-y> — run the probe against the live session and
# leave its output in $output / $status (via bats `run`-style vm_run).
_probe() {
    vm_run "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
        /usr/bin/qdwin-popup-probe --offset-x $1 --offset-y $2 \
        --parent-w 800 --parent-h 600 --popup-w 200 --popup-h 200"
}

# _geom — echo the 4 POPUP_GEOM integers (x y w h) from the first line-anchored
# POPUP_GEOM record in $output, or nothing.
_geom() {
    awk '/^POPUP_GEOM -?[0-9]+ -?[0-9]+ [0-9]+ [0-9]+$/ {print $2, $3, $4, $5; exit}' <<<"$output"
}

@test "popup-clamp: a far off-screen xdg_popup is constrained inside the output" {
    _probe 100000 100000
    assert_success
    assert_output_contains "POPUP_GEOM"
    local geom x y w h
    geom=$(_geom)
    [ -n "$geom" ] || fail_loud "no POPUP_GEOM line in probe output: $output"
    # shellcheck disable=SC2086
    set -- $geom; x=$1 y=$2 w=$3 h=$4

    ensures "a hostile xdg_popup requested 100000px off-screen is clamped fully on-screen (cannot paint over other windows / escape the output)"
    # Engaged AND in-bounds: a positive far offset slides the popup to the far
    # edge, so 0 <= x,y and the popup's far edges land on/inside the output edges
    # (observed x=1720 → x+w=1920, y=849 → y+h=1049 on 1920x1080), never the
    # unclamped echo of ~100000. Also require the popup kept its requested 200x200
    # size so a degenerate response (e.g. POPUP_GEOM 0 0 0 0) can't pass as clamped.
    if [ "$w" -eq 200 ] && [ "$h" -eq 200 ] \
       && [ "$x" -ge 0 ] && [ "$y" -ge 0 ] \
       && [ $((x + w)) -le "$OUTPUT_W" ] && [ $((y + h)) -le "$OUTPUT_H" ]; then
        check_pass "popup clamped within ${OUTPUT_W}x${OUTPUT_H}" "POPUP_GEOM $geom (requested offset 100000,100000)"
    else
        check_fail "w=200, h=200, 0<=x, 0<=y, x+w<=$OUTPUT_W, y+h<=$OUTPUT_H" "POPUP_GEOM $geom" \
            "popup ESCAPED the output — edge clamp not engaged (vendored libweston / constraint kernel regressed)"
    fi
}

@test "popup-clamp: an in-bounds positioner is passed through unclamped" {
    # A modest offset that already fits must NOT be perturbed — the clamp is a
    # backstop, not an unconditional reposition (guards against over-clamping
    # that would misplace legitimate menus).
    _probe 50 50
    assert_success
    local geom x y
    geom=$(_geom)
    [ -n "$geom" ] || fail_loud "no POPUP_GEOM line in probe output: $output"
    # shellcheck disable=SC2086
    set -- $geom; x=$1 y=$2

    ensures "a legitimate popup that already fits on-screen is placed exactly where requested (the clamp does not over-trigger)"
    if [ "$x" -eq 50 ] && [ "$y" -eq 50 ]; then
        check_pass "in-bounds popup unperturbed" "POPUP_GEOM $geom (requested offset 50,50)"
    else
        check_fail "x=50 y=50" "x=$x y=$y" "an in-bounds popup was spuriously moved by the clamp"
    fi
}
