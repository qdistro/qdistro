#!/usr/bin/env bats
# Verifies (and regression-guards) the fixes for the five GUI-test failure
# buckets diagnosed from full-20260619T101414Z:
#   #1 qdistro-print-proxy.service 226/NAMESPACE crash-loop (missing
#      RuntimeDirectory=/run/qdistro-print)
#   #2 qdlocker unlock PAM failure (unix_chkpwd 'user unknown') — in-VM repro
#   #3 broker ApprovalRevoked signal not reaching the wire — real-subscriber repro
#   #4 qdwin-bystander command FIFO landing in /tmp instead of /run/user/1000
#   #5 qdshell vs bystander shell-role contention (start-limit-hit)
#
# One driver, one VM: gui-fixes-verify.sh runs all probes and prints PASS:/FAIL:
# lines plus INFO: diagnostics. See memory: vm_exec_quoting_fragility — drive via
# a staged script, not nested vm-exec quoting.

load helpers

teardown_file() {
    reap_vm_drivers
}

@test "gui-fixes: print-proxy / qdlocker-pam / broker-signal / bystander-fifo / shell-role" {
    stage_vm_driver "gui-fixes-verify.sh"
    vm_run "curl -fsS -o /tmp/gfv.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/gui-fixes-verify.sh && chmod +x /tmp/gfv.sh && bash /tmp/gfv.sh"
    assert_success
    # #1 print-proxy
    assert_output_contains "PASS: print-proxy active after restart"
    assert_output_contains "PASS: print-proxy /run/qdistro-print exists"
    assert_output_contains "PASS: print-proxy no 226/NAMESPACE in last 90s"
    # #4 + #5 bystander FIFO + shell-role
    assert_output_contains "PASS: bystander FIFO at /run/user/1000/qdwin-cmd.fifo"
    assert_output_contains "PASS: bystander bound shell role without contention"
    # #3 broker signal (subscriber must capture it)
    assert_output_contains "PASS: broker ApprovalRevoked captured by real subscriber"
    # #2 qdlocker PAM (standalone repro must accept the standard test password)
    assert_output_contains "PASS: qdlocker PAM accepts Pa_ssw0rd45"
    # #6 qdwin smoke: setsid -f detached test-window reaches qdwin (or at least
    # survives the launching shell — the bug was a bare `&` getting SIGHUP'd).
    assert_output_contains "PASS: smoke: detached qdistro-test-window"
    # #7 qdlocker B1: systemctl --user with XDG_RUNTIME_DIR applies the dropin
    assert_output_contains "PASS: qdlocker B1: systemctl --user (with XDG_RUNTIME_DIR) applied the idle dropin"
    # #X XWayland: the package is installed for the qdwin golden (the load-bearing
    # half of the XWayland fix; weston-load is asserted via INFO/fallback).
    assert_output_contains "PASS: xwayland: /usr/bin/Xwayland installed"
    # Driver ran to completion.
    assert_output_contains "VERIFY-DONE"
    # Surface the driver's INFO: diagnostics (esp. the #2 PAM/sandbox verdict and
    # the #3 captured payload) in the TAP log even when the test passes.
    echo "# ---- gui-fixes-verify driver output ----" >&3
    echo "$output" | grep -E 'PASS:|FAIL:|INFO:' | sed 's/^/# /' >&3
}
