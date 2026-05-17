#!/usr/bin/env bats
# §6.5 per-view RDP regression suite. Each test wraps one reproducible
# probe from scripts/vm/. Assumes the VM has recent code
# installed and pipewire running (see README.md in this dir).

load helpers

setup() {
    vm_run "pgrep -x pipewire >/dev/null"
    assert_success
    # Default: ensure broker is NOT running so the broker-absent tests
    # (s3c-*, s5*, s4-broker-absent-*) see the auto-approve / fail-closed
    # path they were written against. Tests that want broker present
    # (s4-revoke-teardown) start it themselves.
    vm_run "systemctl stop qdistro-admin-broker.service 2>/dev/null || true"
}

@test "s3c-e2e: subscribe + qdistro-forward spawns, RDP port accepts, frames flow" {
    vm_run "bash /root/s3c-e2e.sh"
    assert_success
    assert_output_contains "port 3401 ACCEPTS"
    assert_output_contains "on_stream_process tick frame="
    assert_output_contains "OK: forward reaped"
}

@test "s5a-e2e: claim() accepts a valid token" {
    vm_run "bash /root/s5a-e2e.sh"
    assert_success
    assert_output_contains "claim accepted"
}

@test "s5c-e2e: injected 'hello' reaches focused terminal via per-stream seat" {
    vm_run "bash /root/s5c-e2e.sh"
    assert_success
    assert_output_contains "stream seat 'qdwin-stream-"
    assert_output_contains "captured 'hello' at focused surface"
}

@test "s3c-idle-gate: pulse thread skips request_frame when no RDP peer" {
    vm_run "bash /root/s3c-idle-gate.sh"
    assert_success
    assert_output_contains "PASS — pulse correctly idle when no peer connected"
}

@test "s3c-sdl-dummy: sdl-freerdp with SDL_VIDEODRIVER=dummy stays alive" {
    vm_run "bash /root/s3c-sdl-dummy.sh"
    assert_success
    assert_output_contains "PASS: stayed alive"
}

# Real xfreerdp client coverage. sdl-freerdp dummy driver is a stub —
# it accepts the RDP handshake but bypasses real video decode, so the
# s3c-sdl-dummy test only proves "RDP server is talking". This test
# extends coverage by driving xfreerdp /v:host:port with the
# real-client video path (--auth-only to skip framebuffer rendering
# while still completing TLS + auth + capability exchange, so it
# works headless without an X server). See the TODO comment at
# tiered-isolation.bats:544-546 documenting this gap.
#
# Fails loudly when xfreerdp is missing on the test VM — install
# `freerdp3` (or `freerdp2`) in the bake so the dep is present.
# The companion s3c-xfreerdp.sh helper handles the subscribe-and-
# extract-credentials dance — it lives at
# qdistro/scripts/install/probe-rdp-with-xfreerdp.sh on the host
# and is installed to /root/s3c-xfreerdp.sh on the baked VM.
@test "s3c-xfreerdp: real xfreerdp client completes RDP handshake" {
    vm_run "command -v xfreerdp >/dev/null 2>&1 || command -v xfreerdp3 >/dev/null 2>&1"
    require "xfreerdp not installed on VM (install freerdp3 in the bake)"
    vm_run "test -x /root/s3c-xfreerdp.sh"
    require "/root/s3c-xfreerdp.sh missing — §6.5 helpers not installed by the bake"
    vm_run "bash /root/s3c-xfreerdp.sh"
    assert_success
    assert_output_contains "PASS: xfreerdp handshake completed"
}

@test "s6.7-v2-events: qdshell receives seat/output lifecycle events" {
    vm_run "bash /root/s6-v2-events.sh"
    assert_success
    assert_output_contains "PASS: shell bound at v>=2"
    assert_output_contains "PASS: output_created received"
    assert_output_contains "PASS: §6.7 v2 events probe"
}

@test "s6.7-xdg-activation: token issued, bogus activate silently ignored" {
    vm_run "bash /root/s7-xdg-activation.sh"
    assert_success
    assert_output_contains "PASS: token issued"
    assert_output_contains "PASS: §6.7 xdg-activation-v1 end-to-end"
}

@test "s6.7-protocol-globals: idle-inhibit + ext-idle-notify + cursor-shape + fractional-scale + primary-selection" {
    vm_run "bash /root/s8-protocol-globals.sh"
    assert_success
    assert_output_contains "PASS: idle-inhibit create_inhibitor"
    assert_output_contains "PASS: cursor-shape set_shape(text)"
    assert_output_contains "PASS: fractional-scale preferred_scale=120"
    assert_output_contains "PASS: primary-selection set_selection"
    assert_output_contains "PASS: §6.7 protocol globals end-to-end"
}

@test "s6.7-primary-selection-c: end-to-end data_offer → receive → send via C probe" {
    vm_run "bash /root/s9-primary-selection-c.sh"
    assert_success
    assert_output_contains "B: PASS"
    assert_output_contains "PASS: §6.7 primary-selection end-to-end (C)"
}

@test "s4-broker-absent-optional: qdshell auto-approves with warning log" {
    vm_run "MODE=optional_allow bash /root/s4-broker-gate.sh"
    assert_success
    assert_output_contains "PASS: broker-absent auto-approve with warning log"
}

@test "s4-broker-absent-required: qdshell fails closed when required" {
    vm_run "MODE=required_fail bash /root/s4-broker-gate.sh"
    assert_success
    assert_output_contains "PASS: required mode fails closed"
}

# s4-revoke-teardown needs qdistro-admin-broker.service installed.
# scripts/vm/install-broker-for-qdwin.sh is idempotent and
# bootstraps it; baseweed clones without broker skip this scenario.
# Broker is started here (setup() stops it for the broker-absent tests).
@test "s4-revoke-teardown: admin revoke tears down matching streams" {
    vm_run "test -f /etc/systemd/system/qdistro-admin-broker.service"
    if [[ "$status" -ne 0 ]]; then
        fail_loud "broker not installed on $VM_NAME — see spike-6.5/install-broker-for-qdwin.sh"
    fi
    vm_run "systemctl start qdistro-admin-broker.service"
    assert_success
    vm_run "bash /root/s4-revoke-teardown.sh"
    assert_success
    assert_output_contains "PASS: revoke teardown end-to-end"
}

# P01 (plan2/tasks/P01-greeter-boot-path.md):
# greetd → qdgreeter → qdwin-session.target → qdshell-on-qdwin.
# Smoke driver s100 asserts every step in the boot path including
# "LXQt is NOT running" and the tty4 fallback escape hatch.
@test "greeter-to-qdshell: greetd boots qdgreeter→qdwin→qdshell on tty3" {
    vm_run "test -x /root/s100-greeter-boots-qdshell.sh"
    require "/root/s100-greeter-boots-qdshell.sh missing — see plan2/tasks/P01"
    vm_run "bash /root/s100-greeter-boots-qdshell.sh"
    assert_success
    assert_output_contains "PASS: greetd launched qdgreeter on tty3"
    assert_output_contains "PASS: qdgreeter received password via greetd JSON-IPC"
    assert_output_contains "PASS: qdgreeter handed off to qdwin (pid recorded)"
    assert_output_contains "PASS: qdwin started qdshell-session.target"
    assert_output_contains "PASS: qdshell bound qdwin_shell_v1 v14"
    assert_output_contains "PASS: qdshell panel visible (screenshot OCR found 'system menu')"
    assert_output_contains "PASS: LXQt is NOT running (no labwc / lxqt-panel processes)"
    assert_output_contains "PASS: fallback escape-hatch documented and reachable via tty4"
}
