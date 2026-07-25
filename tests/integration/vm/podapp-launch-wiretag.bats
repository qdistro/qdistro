#!/usr/bin/env bats
# Pod-app launch e2e (tracker J12) — prove a LAUNCHER CLICK on a container app
# produces a fully identified window at the compositor, through the production
# path: qdshell (admin) -> SessionManager1.LaunchPodApp -> the User=root
# qdistro-podapp@<launch-token>.service -> spawn-tier2 in root-launcher mode.
#
# Before J12 the click forked spawn-tier2 straight from the unprivileged
# qdshell session. Two stacked defects followed, and BOTH had to be fixed for a
# tier-2 window to be identifiable at all:
#
#   Drop A (launch side) — no root launcher parent, so qdistro-secctx-exec was
#     never invoked: the container's Wayland connection carried no
#     wp_security_context_v1 at all in dev, and on a hardened profile the
#     launch was refused outright (clicking a pod app did nothing).
#   Drop B (compositor side) — tier-2 windows arrive as NESTED PROXIES, and
#     qdwin's toplevel_security_context sender skipped proxies unconditionally,
#     so even a perfectly tagged container produced no event and the shell's
#     cold-start placeholder always timed out after 15s.
#
# The heavy lifting lives in probes/podapp-launch-wiretag-probe.sh, run as root
# in the VM. It drives the SHIPPED daemon method, unit and helpers with NO dev
# override (no QDWIN_SECCTX_OPEN, no QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED), and
# checks both journal signals carry the SAME token the D-Bus reply returned —
# that token is what the shell matches to resolve its placeholder.

load helpers

PROBE="/root/podapp-launch-wiretag-probe.sh"

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (deploy step)"
    vm_run "command -v podman >/dev/null"
    assert_success || fail_loud "podman not available in the VM"
    vm_run "[ -x /usr/bin/qdistro-tier2-spawn ]"
    assert_success || fail_loud "/usr/bin/qdistro-tier2-spawn not installed (PACKAGING GAP)"
    vm_run "[ -x /usr/libexec/qdistro/qdistro-podapp-launch ] && [ -x /usr/libexec/qdistro/qdistro-podapp-stop ]"
    assert_success || fail_loud "pod-app launch/stop helpers not installed (PACKAGING GAP)"
    vm_run "[ -f /etc/systemd/system/qdistro-podapp@.service ]"
    assert_success || fail_loud "qdistro-podapp@.service not installed (PACKAGING GAP)"
    vm_run "command -v qdistro-secctx-exec >/dev/null"
    assert_success || fail_loud "qdistro-secctx-exec not installed (PACKAGING GAP)"
    vm_run "bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: setup"
}

teardown_file() {
    vm_run "bash $PROBE teardown"
    assert_success || fail_loud "podapp-launch-wiretag teardown failed (a container or unit may persist)"
    assert_output_contains "PASS: teardown"
}

@test "podapp-launch-wiretag: a launcher click stamps <container>/<app> on the wire AND forwards it for the nested proxy" {
    vm_run "bash $PROBE wiretag"
    assert_success
    assert_output_contains "PASS: LaunchPodApp returned a launch token"
    assert_output_contains "PASS: the unit ran and spawn-tier2 used the PRE-COMMITTED token"
    assert_output_contains "PASS: pod-app launch did not downgrade to the un-tagged fallback"
    # Drop A: the tag reached the compositor at all.
    assert_output_contains "PASS: qdwin received the pod-app secctx app_id ON THE WIRE"
    # Drop B: and the compositor told the shell which window it belongs to.
    assert_output_contains "PASS: qdwin forwarded toplevel_security_context for the nested proxy"
    assert_output_contains "PASS: pod-app container lives in admin's rootless podman"
    assert_output_contains "PASS: pod-app container absent from root's podman store"
    assert_output_contains "PASS: systemctl stop -> admin container gone"
    assert_output_contains "PASS: spent launch stanza removed when the unit stopped"
    assert_output_contains "PASS: wiretag"
}

@test "podapp-launch-wiretag: fails closed (no un-tagged pod app, no non-admin root unit)" {
    vm_run "bash $PROBE fail-closed"
    assert_success
    assert_output_contains "PASS: LaunchPodApp refuses a non-admin caller"
    assert_output_contains "PASS: launch helper REFUSES a non-root-owned (admin-writable) launch env"
    assert_output_contains "PASS: launch helper REFUSES a malformed launch token"
    assert_output_contains "PASS: launch helper REFUSES a stanza whose token does not match the unit instance"
    assert_output_contains "PASS: spawn-tier2 REFUSES a malformed pre-committed launch token"
    assert_output_contains "PASS: fail-closed"
}
