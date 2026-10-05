#!/usr/bin/env bats
# §Phase-7 tier 3s (gVisor runsc), todo/paravirt Phase C: the owning scope's
# limits are enforced, not just set (driver s130-tier3s-limits.sh;
# tier3s/CONTRACT.md §3 D-A3b). MemoryMax (OOM kill), TasksMax (fork bomb
# bounded at pids.max), CPUQuota (cpu.stat throttling), plus the
# selective-delegation proof that admin cannot raise any of them and a
# recursive cgroup.procs placement re-proof. Headless staging only.

load helpers
load tier3s

setup_file() {
    t3s_setup_file s130-tier3s-limits.sh
}

teardown_file() {
    t3s_teardown_file
}

@test "phase7-tier3s-limits: scope limits enforced (memory, tasks, cpu), delegation, placement" {
    t3s_run_driver s130-tier3s-limits.sh
    t3s_log s130
    assert_success
    t3s_no_failures s130
    # the contract's limit values on the live scope
    assert_output_contains "PASS: limits: memory.max = MemoryMax=2G"
    assert_output_contains "PASS: limits: memory.swap.max = MemorySwapMax=0"
    assert_output_contains "PASS: limits: pids.max = TasksMax=1024"
    assert_output_contains "PASS: limits: cpu.max = CPUQuota=200%"
    # admin cannot raise any of them — EACCES/EPERM observed, not just rc!=0
    for f in memory.max memory.swap.max pids.max cpu.max; do
        assert_output_contains "PASS: limits: admin write to $f fails with EACCES/EPERM"
    done
    # placement re-proof
    for c in runuser podman-cli conmon runsc-gofer runsc-sandbox runsc-fd-parking systrap-stub; do
        assert_output_contains "PASS: placement[A]: $c x"
    done
    assert_output_contains "PASS: placement[A]: runsc-bundle processes outside the owning scope"
    # the three limits enforce
    assert_output_contains "PASS: cpu: nr_throttled grew under a 400%-hungry load"
    assert_output_contains "PASS: tasks: an outside admin process cannot inject itself into the scope"
    assert_output_contains "PASS: tasks: C's scope really is at TasksMax=1024"
    assert_output_contains "PASS: tasks: host pids.current never exceeded pids.max"
    assert_output_contains "PASS: tasks: pids.events.local max grew"
    assert_output_contains "PASS: tasks: the bomb was bounded"
    assert_output_contains "PASS: memory: B's own memory.max triggered the OOM"
    # everything torn down
    assert_output_contains "PASS: tasks: all "
    assert_output_contains "PASS: memory: all "
    assert_output_contains "PASS: cpu: all "
}
