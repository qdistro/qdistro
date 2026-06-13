#!/usr/bin/env bats
# Disposable secctx WIRE-tag e2e — prove a tier-2 disposable's secctx app_id
# (qdistro.disp.<token>) actually reaches the outer compositor ON THE WIRE via
# wp_security_context_v1, through the PRODUCTION root-launcher path
# (spawn-tier2 TIER2_ROOT_LAUNCHER=1). This closes the residual the M3
# disposables lane (disposables-e2e.bats) deferred to "phase7-secctx": that
# lane ran an ADMIN-driven spawn, which qdwin refuses to tag (no direct root
# launcher parent), so the disposable ran UN-TAGGED.
#
# The deterministic signal is qdwin's OWN secctx commit-handler journal line
# (the same line the tier-3 lane s40-secctx.sh asserts), carrying the secctx
# triple a screenshot could never show:
#   qdwin/secctx: committed engine=qdistro.tier2 app_id=qdistro.disp.<token>
#                 instance_id=<launch-token> ...
#
# Heavy lifting lives in probes/disp-secctx-wiretag-probe.sh (run as root in
# the VM — it IS the root launcher). It drives the SHIPPED
# /usr/bin/qdistro-tier2-spawn --disposable (not a source copy) so a packaging
# gap surfaces, and asserts the inner container runs in admin's ROOTLESS podman
# (no rootful confusion).

load helpers

PROBE="/root/disp-secctx-wiretag-probe.sh"

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (deploy step / fresh-vm-bootstrap probes)"
    vm_run "command -v podman >/dev/null"
    assert_success || fail_loud "podman not available in the VM"
    vm_run "[ -x /usr/bin/qdistro-tier2-spawn ]"
    assert_success || fail_loud "/usr/bin/qdistro-tier2-spawn not installed — disposable spawn path absent (PACKAGING GAP)"
    vm_run "command -v qdistro-secctx-exec >/dev/null"
    assert_success || fail_loud "qdistro-secctx-exec not installed — the wire-tag launcher cannot run (PACKAGING GAP)"
    vm_run "bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: setup"
}

teardown_file() {
    vm_run "bash $PROBE teardown"
    assert_success || fail_loud "disp-secctx-wiretag teardown failed (test-authored broker rule may persist)"
    assert_output_contains "PASS: teardown"
}

@test "disposable-secctx-wiretag: root-launcher spawn stamps qdistro.disp.<token> on the wire (qdwin secctx commit)" {
    vm_run "bash $PROBE wiretag"
    assert_success
    assert_output_contains "PASS: root-launcher spawn computed disp app_id"
    assert_output_contains "PASS: root-launcher path did not downgrade to the un-tagged fallback"
    assert_output_contains "PASS: qdwin received the disposable secctx app_id ON THE WIRE"
    assert_output_contains "PASS: inner container lives in admin's rootless podman"
    assert_output_contains "PASS: container absent from root's podman store"
    assert_output_contains "PASS: close -> container gone"
    assert_output_contains "PASS: wiretag"
}

@test "disposable-secctx-wiretag: root-launcher fails closed (no un-tagged launch) when it cannot stamp the wire tag" {
    vm_run "bash $PROBE fail-closed"
    assert_success
    # The mode exists ONLY for the wire tag: USE_SECCTX=0 or a missing
    # qdistro-secctx-exec must be a hard error, never a silent un-tagged launch.
    assert_output_contains "PASS: root-launcher refuses TIER2_USE_SECCTX=0"
    assert_output_contains "PASS: root-launcher refuses a missing qdistro-secctx-exec"
    assert_output_contains "PASS: fail-closed"
}
