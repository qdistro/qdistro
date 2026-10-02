#!/usr/bin/env bats
# Workflow engine, live in the broker — headless equivalents of
# tests/integration/workflow-gui/01, 02, 03 and 05 (2026-10-02 test audit).
#
# The GUI files assert workflow_audit.sqlite rows, broker D-Bus replies and
# the per-run ssh-agent socket; the admin app's Workflows tab only renders
# ListWorkflows / ListWorkflowRuns. This file asserts those directly through
# tests/integration/vm/s130-workflow-headless.sh, one section per @test:
#
#   wf01  cron trigger -> run -> workflow_runs/workflow_steps rows
#   wf02  deliver_secret -> per-run ssh-agent holds the vault key while the
#         triggering process lives -> socket, dir and agent gone after; no key
#         material in the workflow audit DB or the broker journal
#   wf03  delivery then a failing step -> run failed, secret still scrubbed
#   wf05  no auto_run -> ONE deduped pending run; non-admin approve denied;
#         admin Preview + Approve(digest) runs the same run id to completed
#
# NOT covered here (visual, belongs on the qdwin lane): the Workflows tab
# rendering itself (workflow-gui/04) and wf05 S3's plan-review dialog.

load helpers

teardown_file() {
    reap_vm_drivers
}

run_section() {
    stage_vm_driver "s130-workflow-headless.sh"
    vm_run "curl -fsS -o /tmp/s130.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s130-workflow-headless.sh && chmod +x /tmp/s130.sh && bash /tmp/s130.sh $1 2>/dev/null"
}

@test "wf01: one cron trigger -> one run -> audit row chain" {
    run_section wf01
    assert_success
    assert_output_contains "PASS: wf01: cron trigger fired a run recorded completed in workflow_runs"
    assert_output_contains "PASS: wf01: the run's step row is call_broker|1"
    assert_output_contains "PASS: wf01: ListWorkflows lists wfhl-tick trigger=cron steps=1"
    assert_output_contains "PASS: wf01: ListWorkflowRuns shows a completed wfhl-tick run with a start time"
}

@test "wf02: secret delivery — ephemeral ssh-agent socket exists, then gone" {
    run_section wf02
    assert_success
    assert_output_contains "PASS: vault wfhl unlocked with a fresh ed25519 key"
    assert_output_contains "PASS: wf02: per-run agent socket exists while the process lives (/run/qdistro/workflow-secrets/ssh-"
    assert_output_contains "PASS: wf02: the agent holds the vault key (fingerprint matches)"
    assert_output_contains "PASS: wf02: ListWorkflowRuns shows the run running while wait_for_process blocks"
    assert_output_contains "PASS: wf02: ListWorkflows lists trigger=process_spawn steps=2 needs=vault/wfhl/sign-key"
    assert_output_contains "PASS: wf02: after the process exits the agent socket and its ssh-* dir are gone"
    assert_output_contains "PASS: wf02: the per-run ssh-agent process is gone"
    assert_output_contains "PASS: wf02: the run is completed"
    assert_output_contains "PASS: wf02: no private-key material in the workflow audit DB or the broker journal"
}

@test "wf03: failure mid-step -> secret scrubbed -> run marked failed" {
    run_section wf03
    assert_success
    assert_output_contains "PASS: wf03: the run is recorded failed"
    assert_output_contains "PASS: wf03: steps deliver_secret|1 then call_broker|0 (delivery worked, forbidden call failed)"
    assert_output_contains "PASS: wf03: the delivered secret was scrubbed despite the failure (no agent.sock, no ssh-* dir)"
    assert_output_contains "PASS: wf03: ListWorkflowRuns error column carries the call_broker whitelist refusal"
}

@test "wf05: pending approval queue gates the run" {
    run_section wf05
    assert_success
    assert_output_contains "PASS: wf05: cron fires parked exactly ONE pending run (deduped), none executed"
    assert_output_contains "PASS: wf05: admin PreviewWorkflowRun returned the plan digest"
    assert_output_contains "PASS: wf05: non-admin ApproveWorkflowRun (even with the right digest) denied"
    assert_output_contains "PASS: wf05: admin approve with a wrong digest returns false"
    assert_output_contains "still pending after the refused approvals"
    assert_output_contains "PASS: wf05: admin ApproveWorkflowRun(run, previewed digest) accepted"
    assert_output_contains "executed to completed"
    assert_output_contains "PASS: wf05: exactly one completed run (no auto-run)"
}
