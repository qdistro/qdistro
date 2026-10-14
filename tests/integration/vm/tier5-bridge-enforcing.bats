#!/usr/bin/env bats
# qci:enforcing — runs on a clone of the per-run golden booted
# SELinux=enforcing, reached over SSH (ci/lib/gates/bats.sh).
#
# Tier-5 privileged boundary (todo/review/tier5-decisions, step 8): the
# admin → root step of a tier-5 launch,
#
#   spawn-tier5 (admin) ─ pkexec ─▶ qdistro-tier5-bridge (root)
#       └─ runuser (root) ─ qdistro-secctx-exec (admin) ─ waypipe (admin)
#
# exercised with NO cached grant and WITHOUT the YES polkit test rule the
# other tier-5 drivers install. Authentication goes through the shipped
# polkit action (org.qdistro.tier5.bridge) and the qdistro polkit agent's
# forced PAM route, and its verdict is relayed through the broker's peer
# checks. The password reaches the unmodified agent through a root stand-in
# bind-mounted over its Qt prompt executable (qdistro-polkit-prompt) — only
# the dialog is replaced; PAM verifies the password. The agent's
# QDISTRO_POLKIT_NONINTERACTIVE seam is not used: the broker refuses to relay
# for an agent carrying it. The two CI-VM adaptations (a tty6 logind login session for the
# agent; an AUTH_ADMIN_KEEP rule because that session is not the foreground
# VT, introduced only AFTER the shipped allow_inactive=no refusal is proven)
# are documented in the guest driver s65-tier5-bridge-boundary.sh.
#
# The @tests run in file order and share guest state set up in setup_file;
# each prints its guest transcript to the TAP stream.
# A permissive run of this file is a failure, never a pass.

load helpers

S65=/run/qci-t5b-drv/s65.sh

# s65 <case> — run one driver case as root in the guest; keep the transcript.
s65() {
    vm_run "bash $S65 $1"
    printf '%s\n' "$output" | sed 's/^/# /' >&3
}

setup_file() {
    export VM_NAME VM_EXEC
    start_user_session || fail_loud "admin compositor (wayland-1) did not come up"
    stage_vm_driver "s65-tier5-bridge-boundary.sh"
    vm_run "install -d -m 0700 /run/qci-t5b-drv && curl -fsS -o $S65 http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s65-tier5-bridge-boundary.sh && bash $S65 setup"
    printf '%s\n' "$output" | sed 's/^/# /' >&3
    if [[ "$status" -ne 0 ]]; then
        fail_loud "s65 setup failed (exit $status): $output"
        return 1
    fi
}

teardown_file() {
    vm_run "bash $S65 teardown; rm -rf /run/qci-t5b-drv"
    printf '%s\n' "$output" | sed 's/^/# /' >&3
    reap_vm_drivers
}

@test "tier5-bridge: enforcing release-like posture, shipped action, no cached grant, no test rule" {
    ensures "the boundary is tested as shipped: enforcing, no passwordless sudo, no YES rule, forced PAM, no cached grant"
    s65 posture
    assert_success
    assert_output_contains "PASS: SELinux is Enforcing"
    assert_output_contains "PASS: admin has no passwordless sudo (sudo -n refused)"
    assert_output_contains "PASS: bridge installed root:root 0755 at /usr/libexec/qdistro/qdistro-tier5-bridge"
    assert_output_contains "PASS: bridge starts an isolated interpreter"
    assert_output_contains "PASS: shipped action defaults: any=auth_admin inactive=no active=auth_admin_keep, exec.path pinned"
    assert_output_contains "PASS: no polkit rule (YES test rule or other) names org.qdistro.tier5.bridge"
    assert_output_contains "PASS: installed agent forces the PAM route for org.qdistro.tier5.bridge"
    assert_output_contains "PASS: compositor (pid"
    assert_output_contains "PASS: admin's display session is the inactive tty6 login (adaptation 1)"
    assert_output_contains "PASS: agent has no seam and the session holds no cached grant for org.qdistro.tier5.bridge"
}

@test "tier5-bridge: a caller outside any admin session is refused (no agent, nothing launched)" {
    ensures "a process outside the admin's login session cannot start the root bridge"
    s65 no-session
    assert_success
    assert_output_contains "PASS: no-session: pkexec refused (rc=127"
    assert_output_contains "PASS: no-session: the bridge never ran"
    assert_output_contains "PASS: no-session: no agent authentication started"
    assert_output_contains "PASS: no-session: no launch record registered"
    assert_output_contains "PASS: no-session: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
}

@test "tier5-bridge: an inactive session is refused by the shipped allow_inactive=no (no prompt)" {
    ensures "the shipped policy refuses an inactive session outright"
    s65 inactive
    assert_success
    assert_output_contains "PASS: inactive session: pkexec refused (rc=127"
    assert_output_contains "PASS: inactive: polkitd resolved the caller to the inactive display session"
    assert_output_contains "PASS: inactive: refused without any authentication attempt (allow_inactive=no)"
    assert_output_contains "PASS: inactive: no launch record registered"
    assert_output_contains "PASS: inactive: compositor saw no tier-5 secctx commit"
    assert_output_contains "PASS: inactive: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
}

@test "tier5-bridge: cancelled authentication launches nothing and leaves no grant" {
    ensures "cancelling the password prompt denies the launch"
    s65 cancel
    assert_success
    assert_output_contains "PASS: cancel: no cached grant before the attempt"
    assert_output_contains "PASS: cancelled authentication: pkexec refused (rc=127"
    assert_output_contains "PASS: cancel: agent took the forced PAM route"
    assert_output_contains "PASS: cancel: agent logged '-> deny (prompt-cancelled)'"
    assert_output_contains "PASS: cancel: no launch record registered"
    assert_output_contains "PASS: cancel: the cancelled attempt left no grant"
    assert_output_contains "PASS: cancel: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
}

@test "tier5-bridge: a wrong password is rejected by PAM and launches nothing" {
    ensures "only the admin's real password authorizes the bridge"
    s65 wrong-password
    assert_success
    assert_output_contains "PASS: wrong password: pkexec refused (rc=127"
    assert_output_contains "PASS: wrong-password: PAM rejected it"
    assert_output_contains "PASS: wrong-password: no launch record registered"
    assert_output_contains "PASS: wrong-password: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
}

@test "tier5-bridge: the admin password via PAM authorizes; the launch is tagged and registered" {
    ensures "an authenticated tier-5 launch is tagged by the compositor and registered with the broker under its attested identity"
    s65 auth-success
    assert_success
    assert_output_contains "PASS: auth-success: spawn reported the bridge ready"
    assert_output_contains "PASS: auth-success: agent verified the password through PAM ('-> allow (pam-ok)')"
    assert_output_contains "PASS: auth-success: chain admin spawn("
    assert_output_contains "PASS: auth-success: compositor tagged the client engine=qdistro.tier5 app_id=qdistro.tier5.loopback-"
    assert_output_contains "PASS: auth-success: broker registered pid="
    assert_output_contains "PASS: auth-success: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
}

@test "tier5-bridge: the kept grant serves the same caller process without a second prompt" {
    ensures "auth_admin_keep retains authorization for the caller that authenticated"
    s65 keep-same-caller
    assert_success
    assert_output_contains "PASS: keep: first run authenticated and reached the bridge"
    assert_output_contains "PASS: keep: first run authenticated by PAM"
    assert_output_contains "PASS: keep: second run from the same caller launched (READY"
    assert_output_contains "PASS: keep: no new authentication for the second run (kept grant)"
    assert_output_contains "PASS: keep: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
}

@test "tier5-bridge: admin cannot RegisterLaunch on the broker directly" {
    ensures "an admin process cannot mint a tier-5 launch record without the bridge"
    s65 admin-register
    assert_success
    assert_output_contains "PASS: admin-register: admin RegisterLaunch refused"
    assert_output_contains "PASS: admin-register: no record for vm-s65forge"
}

@test "tier5-bridge: an admin-parent qdistro-secctx-exec launch is refused by the helper and the compositor" {
    ensures "an admin process cannot get a tier-5 secctx tag without the root launcher parent"
    s65 admin-parent
    assert_success
    assert_output_contains "PASS: admin-parent: secctx-exec refuses the marker under an admin parent"
    assert_output_contains "PASS: admin-parent: dev-override run failed"
    assert_output_contains "PASS: admin-parent: compositor committed no context for the admin-parent helpers"
}

@test "tier5-bridge: the bridge refuses a direct non-pkexec invocation" {
    ensures "the bridge acts only as root for the admin via pkexec"
    s65 direct-invoke
    assert_success
    assert_output_contains "PASS: direct-invoke: admin exec of the bridge refused (rc=2)"
    assert_output_contains "PASS: direct-invoke: a non-admin PKEXEC_UID is refused (rc=2)"
    assert_output_contains "PASS: direct-invoke: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
}

@test "tier5-bridge: malformed arguments are refused before anything is launched" {
    ensures "the root bridge accepts only bounded name/port/token arguments"
    s65 malformed
    assert_success
    assert_output_contains "PASS: malformed: all 15 malformed argv sets refused by the bridge"
    assert_output_contains "PASS: malformed: no launch record registered"
    assert_output_contains "PASS: malformed: compositor saw no tier-5 secctx commit"
    assert_output_contains "PASS: malformed: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
}

@test "tier5-bridge: the caller's environment, PATH and cwd cannot reach the root chain" {
    ensures "no caller-controlled variable, executable or descriptor crosses into the root launch chain"
    s65 env-injection
    assert_success
    assert_output_contains "PASS: env-injection: the bridge launched despite the hostile caller environment"
    assert_output_contains "PASS: env-injection: no caller-planted executable, site hook or BASH_ENV ran"
    assert_output_contains "PASS: env-injection: waypipe environment is exactly the bridge's"
    assert_output_contains "PASS: env-injection: secctx-exec got only the constructed environment"
    assert_output_contains "PASS: env-injection: bridge environment carries no caller variable; bridge and waypipe cwd=/"
    assert_output_contains "PASS: env-injection: the fixed /usr/bin/qdistro-secctx-exec and /usr/bin/waypipe ran"
    assert_output_contains "PASS: env-injection: waypipe shares no pipe with the root bridge and carries no QDISTRO_ variable"
    assert_output_contains "PASS: env-injection: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
}

@test "tier5-bridge: a foreign, stale, init, self or garbage pid report is refused" {
    ensures "the bridge registers only the waypipe client it launched, and never signals a foreign process"
    s65 pid-report
    assert_success
    for mode in foreign stale pid1 self garbage; do
        assert_output_contains "PASS: pid-report $mode: refused (rc="
        assert_output_contains "PASS: pid-report $mode: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
    done
    assert_output_contains "PASS: pid-report: no launch record registered"
    assert_output_contains "PASS: pid-report: the foreign admin process"
}

@test "tier5-bridge: an exec failure of the waypipe client registers nothing" {
    ensures "a launch whose client never executes leaves no record, listener or helper"
    s65 exec-failure
    assert_success
    assert_output_contains "PASS: exec-failure: bridge saw no launch report and failed (rc=4)"
    assert_output_contains "PASS: exec-failure: the real secctx-exec reported the failed execvp"
    assert_output_contains "PASS: exec-failure: no launch record registered"
    assert_output_contains "PASS: exec-failure: no secctx listener socket left behind"
    assert_output_contains "PASS: exec-failure: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
}

@test "tier5-bridge: a broker failure aborts the launch and tears the chain down" {
    ensures "an unregistered tier-5 client never stays on the desktop"
    s65 broker-failure
    assert_success
    assert_output_contains "PASS: broker-failure: caller authenticated"
    assert_output_contains "PASS: broker-failure: broker stopped and masked"
    assert_output_contains "PASS: broker-failure: launch aborted on RegisterLaunch failure (rc=6, no READY)"
    assert_output_contains "PASS: broker-failure: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
    assert_output_contains "PASS: broker-failure: broker restored"
}

@test "tier5-bridge: caller death during launch and after READY tears the chain down" {
    ensures "a dead caller never leaves a root bridge or a tagged waypipe client behind"
    s65 caller-death
    assert_success
    assert_output_contains "PASS: caller-death during launch: bridge noticed the caller's EOF"
    assert_output_contains "PASS: caller-death during launch: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
    assert_output_contains "PASS: caller-death after READY: waypipe client"
    assert_output_contains "PASS: caller-death after READY: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
    assert_output_contains "PASS: caller-death (spawn-tier5 SIGKILL): bridge saw EOF and reaped waypipe"
    assert_output_contains "PASS: caller-death (spawn-tier5 SIGKILL): no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
}

@test "tier5-bridge: postconditions — no root file operations in admin-writable paths, no surviving helpers" {
    ensures "the boundary left no root-owned files where admin writes, no root or waypipe helper, and the fixed executables intact"
    s65 postconditions
    assert_success
    assert_output_contains "PASS: secctx-exec, waypipe, bridge, dbus-send, pkexec unchanged and not overmounted"
    assert_output_contains "PASS: no tier-5 bridge chain process (root or admin) survives the file"
    assert_output_contains "PASS: no root-owned file created in admin-writable paths"
    assert_output_contains "PASS: no root-run tier-5 lifecycle state"
    assert_output_contains "PASS: no secctx listener socket from this file left in"
}

@test "tier5-bridge: no AVC denial names the tier-5 launch chain since boot" {
    # Runs last (bats keeps file order) so every case above ran under
    # enforcing. avc-denials.sh fails closed (exit 3) when the audit trail
    # cannot be read.
    stage_vm_driver "probes/avc-denials.sh"
    vm_run "curl -fsS -o /run/qci-t5b-drv/avc-denials.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/avc-denials.sh && bash /run/qci-t5b-drv/avc-denials.sh 'comm=\"(waypipe|qdistro-secctx|pkexec|runuser|qdistro-tier5-b|polkitd)\"'"
    echo "$output"
    [ "$status" -eq 0 ] || fail_loud "tier-5 chain AVC check failed (status=$status)"
    assert_output_contains "AVC-CLEAN"
}
