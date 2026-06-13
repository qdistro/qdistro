#!/usr/bin/env bats
# Persistent templated tier-2 SILO secctx WIRE-tag e2e — prove a silo's secctx
# app_id (qdistro-silo-<name>/<app>) reaches the outer compositor ON THE WIRE
# via wp_security_context_v1, through the PRODUCTION launch path: the
# qdistro-tier2-silo@<name>.service unit (now User=root) -> qdistro-tier2-silo-
# launch -> spawn-tier2 in root-launcher mode (TIER2_ROOT_LAUNCHER=1). This is
# the silo analogue of disposable-secctx-wiretag.bats and closes the residual
# the qdistro-tier2-silo@.service comment flagged: the old User=admin unit ran
# the silo UN-TAGGED (no direct root launcher parent), tagged only under the
# dev override the test VMs set.
#
# The deterministic signal is qdwin's OWN secctx commit-handler journal line:
#   qdwin/secctx: committed engine=qdistro.tier2 app_id=qdistro-silo-<name>/<app>
#                 instance_id=<launch-token> ...
#
# Heavy lifting lives in probes/tier2-silo-secctx-wiretag-probe.sh (run as root
# in the VM — the unit IS the root launcher). It drives the SHIPPED unit +
# /usr/bin/qdistro-tier2-spawn, asserts the inner container runs in admin's
# ROOTLESS podman, asserts the binding resolver ran AS ADMIN (the root-launcher
# resolver drop), and asserts the root-unit ExecStop drops to admin to tear the
# admin container down. NO dev override (QDWIN_SECCTX_OPEN / *_ALLOW_UNTRUSTED).

load helpers

PROBE="/root/tier2-silo-secctx-wiretag-probe.sh"

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (deploy step)"
    vm_run "command -v podman >/dev/null"
    assert_success || fail_loud "podman not available in the VM"
    vm_run "[ -x /usr/bin/qdistro-tier2-spawn ]"
    assert_success || fail_loud "/usr/bin/qdistro-tier2-spawn not installed (PACKAGING GAP)"
    vm_run "[ -x /usr/libexec/qdistro/qdistro-tier2-silo-launch ] && [ -x /usr/libexec/qdistro/qdistro-tier2-silo-stop ]"
    assert_success || fail_loud "silo launch/stop helpers not installed (PACKAGING GAP)"
    vm_run "command -v qdistro-secctx-exec >/dev/null"
    assert_success || fail_loud "qdistro-secctx-exec not installed (PACKAGING GAP)"
    vm_run "bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: setup"
}

teardown_file() {
    vm_run "bash $PROBE teardown"
    assert_success || fail_loud "tier2-silo-secctx-wiretag teardown failed (test-authored rule/binding may persist)"
    assert_output_contains "PASS: teardown"
}

@test "tier2-silo-secctx-wiretag: a real binding-resolved silo stamps qdistro-silo-<name>/<app> on the wire AND resolves its binding AS ADMIN" {
    vm_run "bash $PROBE wiretag"
    assert_success
    assert_output_contains "PASS: silo unit computed the silo secctx app_id"
    assert_output_contains "PASS: silo path did not downgrade to the un-tagged fallback"
    assert_output_contains "PASS: binding resolver ran AS ADMIN"
    assert_output_contains "PASS: qdwin received the SILO secctx app_id ON THE WIRE"
    assert_output_contains "PASS: silo container lives in admin's rootless podman"
    assert_output_contains "PASS: silo container absent from root's podman store"
    assert_output_contains "PASS: systemctl stop -> admin container gone"
    assert_output_contains "PASS: wiretag"
}

@test "tier2-silo-secctx-wiretag: fails closed (no un-tagged silo) when it cannot stamp / drop" {
    vm_run "bash $PROBE fail-closed"
    assert_success
    assert_output_contains "PASS: launch helper refuses a missing admin user"
    assert_output_contains "PASS: stop helper refuses an admin user that resolves to uid 0"
    assert_output_contains "PASS: spawn-tier2 root-launcher refuses TIER2_USE_SECCTX=0"
    assert_output_contains "PASS: fail-closed"
}
