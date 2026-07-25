#!/usr/bin/env bats
# VM-gated runtime guard for the locked-session VT escape.
#
# The static invariants in bootstrap-hardening.bats only grep the installers.
# The property that actually protects a locked screen is a RUNTIME one: the
# compositor VT's console keyboard is K_OFF, and nothing can take the VT and
# reset it. `systemctl is-enabled` cannot show that — logind starts
# autovt@ttyN BY UNIT NAME on demand, so "disabled" proves nothing. This lane
# runs probes/vt-escape-lockdown.sh inside the VM to measure it.
#
# Why it matters: if a getty takes the compositor VT, its start-time TTY reset
# reverts seatd's K_OFF; openSUSE's console keymap then makes Super+Left a
# Decr_Console switch, and the unlock password typed at the lock screen lands
# in login(1) — recorded in cleartext as a failed-login *username* in the
# journal and btmp.
#
# The destructive freed-VT experiment (stop greetd, free the VT, switch away
# and back, assert no getty appears) is opt-in via QDISTRO_VT_FREED_EXPERIMENT=1
# because it tears down the running session. It is the only check that
# exercises the actual regression path, so run it on a disposable VM.
load helpers

teardown_file() {
    reap_vm_drivers
}

@test "vt-escape: the compositor VT is K_OFF and cannot be taken by a getty" {
    stage_vm_driver "probes/vt-escape-lockdown.sh"
    vm_run "curl -fsS -o /tmp/vt-escape-lockdown.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/vt-escape-lockdown.sh && chmod +x /tmp/vt-escape-lockdown.sh && bash /tmp/vt-escape-lockdown.sh"
    assert_success

    assert_output_contains "PASS: active VT is tty"
    # The load-bearing measurement: K_OFF (KDGKBMODE 4) on the compositor VT.
    assert_output_contains "PASS: tty3 console keyboard is K_OFF"
    # Masked, not merely disabled — logind ignores enablement for autovt@.
    assert_output_contains "PASS: getty@tty3 + autovt@tty3 are masked and inactive"
    # ...and tty1's deliberate emergency console (doc/recovery.md) survived.
    assert_output_contains "PASS: tty1 emergency agetty left intact"
    assert_output_contains "PASS: locked-session VT escape is closed on tty3 (static checks only)"
}

@test "vt-escape: a freed compositor VT gets no login prompt (destructive)" {
    [ "${QDISTRO_VT_FREED_EXPERIMENT:-0}" = "1" ] \
        || skip "set QDISTRO_VT_FREED_EXPERIMENT=1 (stops greetd; disposable VM only)"

    stage_vm_driver "probes/vt-escape-lockdown.sh"
    vm_run "curl -fsS -o /tmp/vt-escape-lockdown.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/vt-escape-lockdown.sh && chmod +x /tmp/vt-escape-lockdown.sh && bash /tmp/vt-escape-lockdown.sh --with-freed-vt-experiment"
    assert_success

    # Proves the experiment was not vacuous: the VT really did change, so
    # logind really was asked to put a getty on the free compositor VT.
    assert_output_contains "PASS: switched away from and back to the freed tty3 (logind was asked)"
    assert_output_contains "PASS: no login prompt spawned on the freed compositor VT"
    assert_output_contains "PASS: greetd recovered with the mask in place"
    assert_output_contains "PASS: locked-session VT escape is closed on tty3 (with freed-VT experiment)"
}
