#!/usr/bin/env bats
# §Bridge Phase-9e — desktop-integration daemons end-to-end.
#
# REQUIRES A LIVE VM (qdwin guest with a real browser + the four 9e
# daemons installed). This file is NOT run in the headless host unit-
# test pass — `python -m pytest tests/unit/test_browser_daemons_9e.py`
# covers the pure dispatch logic; this scenario verifies the bus wiring
# that only a running session bus + browser can exercise:
#
#   1. The four well-known names are actually owned on the user session
#      bus after the daemons start (the gap that returned
#      dbus_call_failed before Phase-9e).
#   2. A browser playing media surfaces an
#      org.mpris.MediaPlayer2.qdistro.<user>.<browser> player the admin
#      media widget can see.
#   3. A completed download raises a notification whose action opens the
#      originating user's file manager.
#   4. Fullscreen video holds a logind idle inhibitor that is released
#      on exit.
#
# Run only when a VM is provisioned, e.g.:
#   VM_NAME=qdwin-9e bats tests/integration/vm/browser-9e-daemons.bats
#
# The heavy scenario lives in s-browser-9e-daemons.sh so vm-exec's qga
# JSON quoting can't mangle the nested busctl payloads (same pattern as
# the other vm/*.bats drivers).

load helpers

setup() {
    # Bring up the per-user 9e daemons inside the VM's graphical session.
    # They are socket/dbus-activated, so a busctl introspect is enough to
    # spawn them; we start them explicitly here to fail loud if a unit is
    # missing rather than silently skipping.
    # These are admin's (uid 1000) per-user units; `systemctl --user` only
    # reaches them as that user. Under qga, vm_run runs as root with no
    # XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS, so a root `systemctl --user`
    # fails to connect to any user bus — use vm_run_admin like the rest of the
    # --user suite (and the @test/wait_for_bus_name below).
    vm_run_admin "systemctl --user is-active --quiet qdistro-mpris.service \
            || systemctl --user start qdistro-mpris.service"
    vm_run_admin "systemctl --user is-active --quiet qdistro-downloads.service \
            || systemctl --user start qdistro-downloads.service"
    vm_run_admin "systemctl --user is-active --quiet qdistro-notifications.service \
            || systemctl --user start qdistro-notifications.service"
    vm_run_admin "systemctl --user is-active --quiet qdistro-compositor.service \
            || systemctl --user start qdistro-compositor.service"

    # `systemctl start` of a Type=dbus unit returns once the BusName is
    # acquired, but a freshly-provisioned session can bounce a per-user
    # daemon once during bring-up, so a well-known name can briefly be
    # unowned right when the @test below introspects the bus. Wait for all
    # four to settle so the single-shot busctl check can't race that window
    # (previously dropped org.qdistro.Compositor — the last one started).
    wait_for_bus_name org.qdistro.Mpris         --user
    wait_for_bus_name org.qdistro.Downloads     --user
    wait_for_bus_name org.qdistro.Notifications --user
    wait_for_bus_name org.qdistro.Compositor    --user
}

@test "9e-daemons: four bus names owned on the user session bus" {
    # vm_run/vm_run_admin already wrap the command in bats `run`, setting
    # $status/$output in this scope; call it bare (a second `run` would
    # capture vm_run_admin's own empty stdout and clobber $output).
    vm_run_admin "busctl --user list | grep -Eo \
        'org.qdistro.(Mpris|Downloads|Notifications|Compositor)' | sort -u"
    [ "$status" -eq 0 ]
    [[ "$output" == *"org.qdistro.Compositor"* ]]
    [[ "$output" == *"org.qdistro.Downloads"* ]]
    [[ "$output" == *"org.qdistro.Mpris"* ]]
    [[ "$output" == *"org.qdistro.Notifications"* ]]
}

@test "9e-daemons: full desktop-integration round-trip" {
    local script_path
    script_path="$(dirname "$BATS_TEST_FILENAME")/s-browser-9e-daemons.sh"
    [ -f "$script_path" ] || skip "driver script not provisioned: $script_path"

    cp "$script_path" "$(dirname "$BATS_TEST_FILENAME")/../"
    stage_http_8765 "$(dirname "$BATS_TEST_FILENAME")/.."

    run vm_run "curl -s http://$(vm_host_ip):8765/s-browser-9e-daemons.sh \
                -o /tmp/s-9e.sh && bash /tmp/s-9e.sh"
    echo "$output"
    [ "$status" -eq 0 ]
    # Load-bearing PASS strings the driver prints; dropping one means the
    # scenario regressed.
    [[ "$output" == *"PASS mpris-player-exported"* ]]
    [[ "$output" == *"PASS download-notification-shown"* ]]
    [[ "$output" == *"PASS notification-policy-gate"* ]]
    [[ "$output" == *"PASS screenlock-inhibit-held"* ]]
    [[ "$output" == *"PASS screenlock-released"* ]]
}
