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
# THIS TEST CONVERTS ITS OWN VM TO THE PRODUCTION PATH FIRST.
#
# The bats lane's VM is not a production box: fresh-vm-bootstrap.sh masks
# greetd (it would grab the DRM seat and block admin's user manager), and the
# greetd PACKAGE's /etc/greetd/config.toml says `vt = next`, which is dynamic
# by design and deliberately unparseable by the probe. A first version of this
# file ignored that and asserted straight against the unconverted VM; it could
# never pass, and failed for the whole of its first CI run (2026-07-26).
#
# So setup_file() runs enable-qdgreeter.sh — the same production provisioner
# `qci snapshot-daily` uses, which installs deploy/greetd-config.toml (vt = 3)
# and, since 2026-07-27, runs harden-compositor-vt.sh fail-closed.
#
# It then installs a TEST-ONLY [initial_session] autologin on this disposable
# VM. That is not cosmetic. Without it greetd's only session is the _greeter
# one, so tty3 is owned by qdgreeter and the probe would measure the login
# chrome rather than the qdwin session whose LOCKED screen is the actual
# subject. deploy/greetd-config.toml documents this exact autologin stanza and
# ships it commented out precisely because it is a test/kiosk affordance.
load helpers

setup_file() {
    export VM_NAME VM_EXEC
    _vt_require_production_path
}

teardown_file() {
    reap_vm_drivers
}

# Convert this disposable VM to the production greetd path and wait for a real
# qdwin session on tty3. Every step is fail-loud: a silent fallback here would
# turn the whole file into a test of the wrong machine.
_vt_require_production_path() {
    vm_run 'test -x /root/qdistro-src/qdistro/scripts/vm/enable-qdgreeter.sh'
    [ "$status" -eq 0 ] || \
        fail_loud "enable-qdgreeter.sh not staged in the VM at /root/qdistro-src"

    # Hand the DRM seat over before greetd asks for it.
    #
    # fresh-vm-bootstrap.sh masks greetd for a concrete reason, quoted from
    # its own comment: "greetd would grab the DRM seat and prevent admin's
    # user manager from starting qdwin-compositor.service ('Device or
    # resource busy' from libseat)". That VM boots admin's lingering user
    # manager straight into qdwin-session.target on wayland-1, so the seat is
    # already taken. Unmasking greetd without stopping that session gives two
    # compositors racing for one seat, and whichever loses fails with EBUSY.
    #
    # Stopping admin's session first is safe here: this file gets its own
    # disposable VM (ci/lib/gates/bats.sh acquires one per .bats file), and
    # greetd's autologin below brings the same units back up — this time
    # through the production path, with a PAM session, on tty3.
    vm_run 'runuser -l admin -c "XDG_RUNTIME_DIR=/run/user/1000 systemctl --user stop qdlocker.service qdwin-session.target qdwin-compositor.service" 2>/dev/null; true'
    # Wait for the seat to actually be released, not just for the stop to
    # return — libseat hands it over asynchronously and greetd would race it.
    local _i
    for _i in $(seq 1 30); do
        vm_run 'pgrep -x weston >/dev/null 2>&1 || test ! -S /run/user/1000/wayland-1'
        [ "$status" -eq 0 ] && break
        sleep 1
    done

    # greetd is masked by fresh-vm-bootstrap.sh; unmask before enabling it.
    vm_run 'systemctl unmask greetd.service 2>/dev/null; true'

    vm_run 'bash /root/qdistro-src/qdistro/scripts/vm/enable-qdgreeter.sh'
    [ "$status" -eq 0 ] || \
        fail_loud "enable-qdgreeter.sh failed (rc=$status): $output"

    # Test-only autologin so a REAL qdwin session owns tty3. Idempotent.
    vm_run 'grep -q "^\[initial_session\]" /etc/greetd/config.toml || cat >>/etc/greetd/config.toml <<'"'"'EOF'"'"'

[initial_session]
user = "admin"
command = "/usr/local/bin/qdwin-session-launcher"
EOF'
    [ "$status" -eq 0 ] || fail_loud "could not install the test autologin: $output"

    # initial_session is intentionally one-shot per boot.  Restarting greetd
    # after this VM has already run an initial session falls back to the
    # greeter, even though the stanza is present.  Reboot the disposable VM so
    # the conversion exercises the real production ordering: greetd takes
    # tty3 first, then its PAM session starts the user's qdwin target.
    vm_run 'cat /proc/sys/kernel/random/boot_id'
    [ "$status" -eq 0 ] || fail_loud "could not read the pre-reboot boot id: $output"
    local _old_boot=${output//[[:space:]]/}
    vm_run 'systemctl reboot'
    sleep 3
    for _i in $(seq 1 60); do
        vm_run 'cat /proc/sys/kernel/random/boot_id'
        if [ "$status" -eq 0 ] && \
           [ "${output//[[:space:]]/}" != "$_old_boot" ]; then
            break
        fi
        sleep 2
    done
    [ "$status" -eq 0 ] && [ "${output//[[:space:]]/}" != "$_old_boot" ] \
        || fail_loud "the converted VM did not complete its production-path reboot"

    # Wait for the session the probe is actually about, not merely for greetd:
    # the compositor active, qdlocker active, AND tty3 actually current.
    # Waiting on greetd alone would let the probe measure the greeter, or a
    # half-started session. qdlocker matters because K_OFF is what keeps a
    # password typed AT THE LOCK SCREEN out of the kernel console — without
    # the locker in the picture this file would be overclaiming.
    for _i in $(seq 1 60); do
        vm_run 'runuser -l admin -c "XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active qdwin-compositor.service qdlocker.service" 2>/dev/null'
        if [ "$status" -eq 0 ] && [ "${output//[[:space:]$'\n']/}" = activeactive ]; then
            vm_run 'cat /sys/class/tty/tty0/active'
            [ "${output//[[:space:]]/}" = tty3 ] && return 0
        fi
        sleep 2
    done
    vm_run 'systemctl status greetd.service --no-pager 2>&1 | tail -30; journalctl -b -u greetd --no-pager 2>&1 | tail -40'
    fail_loud "the converted VM never reached an active qdwin+qdlocker session on tty3: $output"
}

@test "vt-escape: the converted VM really is on the production path" {
    # Guards against the whole file going vacuous. If tty3 stops being the
    # compositor's, or the greetd config drifts back to the package's
    # `vt = next`, the assertions below would be measuring something else.
    #
    # NOTE ON SCOPE: setup_file waits for qdlocker.service to be ACTIVE, which
    # means the locker is running and owns the lock path — it does not drive
    # the session into a locked state. K_OFF is a property of the compositor's
    # VT rather than of the lock overlay, so the measurement below holds
    # either way; what the locked screen adds is the reason anyone cares.
    vm_run 'awk "/^\[terminal\]/{t=1;next} /^\[/{t=0} t&&/^[[:space:]]*vt[[:space:]]*=/{print \$3}" /etc/greetd/config.toml'
    assert_success
    assert_output_contains "3"

    vm_run 'cat /sys/class/tty/tty0/active'
    assert_success
    assert_output_contains "tty3"
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
