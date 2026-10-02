#!/usr/bin/env bats
# Headless permissions suite — the broker-only permissions-gui scenarios,
# without a GUI VM, an LLM agent or a screenshot gate.
#
# The test-relevance audit (todo/test-audit-261002/audit-pg-01-30.md,
# audit-pg-31-60.md) found that these labwc-lane scenarios assert nothing
# visual that matters: their hard checks are D-Bus replies, error names,
# sqlite rows in the broker's approvals/audit stores, signals and rules.d
# files. Where a scenario's GUI step was only "admin presses Approve/Deny in
# the admin app", the drivers make the identical broker call the app makes
# (DecideRequest as the admin uid). Everything goes to the REAL broker on the
# system bus of a disposable VM.
#
# Each @test name starts with the permissions-gui scenario id it replaces.
# Drivers (run inside the VM as root, staged over the bats HTTP stager):
#   s120-perm-lib.sh       shared helpers (state isolation + restore on EXIT)
#   s121-perm-rules.sh     pg24..pg29  declarative rules
#   s122-perm-cache.sh     pg07 pg23 pg30..pg33  cache / revoke / rate limit
#   s123-perm-gates.sh     pg36..pg42  clipboard / handoff gates, ListRules, SaveRule
#   s124-perm-lineage.sh   pg58 pg59   lineage_enforce attestation
#   s125-sendto-xuser.sh   pg11 pg15 pg17  cross-uid send-to via user relays
# Every case restores the broker state it changed (rules.d, broker.conf), so
# the cases are order-independent.

load helpers

setup_file() {
    vm_run "systemctl is-active --quiet qdistro-admin-broker.service \
            || systemctl start qdistro-admin-broker.service"
}

teardown_file() {
    reap_vm_drivers
}

# run_case <driver> <case> — stage lib + driver, run the case in the VM.
run_case() {
    local drv=$1 case=$2 short
    short=${drv%%-*}
    stage_vm_driver "s120-perm-lib.sh"
    stage_vm_driver "$drv"
    vm_run "mkdir -p /tmp/s120d && cd /tmp/s120d && \
            curl -fsS -o s120-perm-lib.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s120-perm-lib.sh && \
            curl -fsS -o $drv http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/$drv && \
            bash /tmp/s120d/$drv $case"
    echo "$output"
    assert_success
    assert_output_contains "PASS: $short $case complete"
    refute_fail_lines
}

# No case may print a FAIL line even if its exit status were lost.
refute_fail_lines() {
    if grep -q '^FAIL: ' <<<"$output"; then
        fail_loud "driver printed FAIL lines"
    fi
}

# --- s121: declarative rules ------------------------------------------------

@test "pg24-rule-allow-no-prompt: SaveRule allow -> ALLOWED, no pending, audit source=rule, no cache row" {
    run_case s121-perm-rules.sh pg24
    assert_output_contains "PASS: pg24: SDK request as work returns ALLOWED (rc 0)"
    assert_output_contains "PASS: pg24: no pending request was enqueued"
    assert_output_contains "PASS: pg24: audit row is uid 2000, decision 1, source rule"
    assert_output_contains "PASS: pg24: rule decisions write no cache row"
}

@test "pg25-rule-deny-short-circuit: SaveRule deny -> DENIED, no pending, audit decision=0 source=rule" {
    run_case s121-perm-rules.sh pg25
    assert_output_contains "PASS: pg25: SDK request as work returns DENIED (rc 1)"
    assert_output_contains "PASS: pg25: no pending request was enqueued"
    assert_output_contains "PASS: pg25: audit row is uid 2000, decision 0, source rule"
    assert_output_contains "PASS: pg25: rule decisions write no cache row"
}

@test "pg26-rule-hot-reload-inotify: file drop (no SaveRule) -> RulesReloaded >=1, ListRules, rule live" {
    run_case s121-perm-rules.sh pg26
    assert_output_contains "PASS: rules baseline isolated: ReloadRules counted 0 rules, no errors"
    assert_output_contains "PASS: pg26: post-drop RulesReloaded signal with count >= 1"
    assert_output_contains "PASS: pg26: ListRules reports the dropped rule"
    assert_output_contains "PASS: pg26: rule is live — work SDK request ALLOWED"
}

@test "pg27-rule-first-match-wins: sorted-filename precedence allow-then-deny and swapped" {
    run_case s121-perm-rules.sh pg27
    assert_output_contains "PASS: pg27: allow-file sorts first -> ALLOWED"
    assert_output_contains "PASS: pg27: audit 1|rule|27a-allow.yaml"
    assert_output_contains "PASS: pg27: deny-file now sorts first -> DENIED"
    assert_output_contains "PASS: pg27: audit 0|rule|27a-deny.yaml"
}

@test "pg28-rule-exe-glob-match: /usr/bin/python* allows python, dbus-send falls through to prompt" {
    run_case s121-perm-rules.sh pg28
    assert_output_contains "PASS: pg28: python caller matches /usr/bin/python* -> ALLOWED"
    assert_output_contains "PASS: pg28: dbus-send caller falls through to a pending prompt"
    assert_output_contains "PASS: pg28: the alt-exe waiter observes the deny"
}

@test "pg29-checkpermission-unknown-fastpath: unknown (no pending/audit/cache), rule allow, cache deny" {
    run_case s121-perm-rules.sh pg29
    assert_output_contains 'PASS: pg29: CheckPermission with no rule/cache returns "unknown"'
    assert_output_contains "PASS: pg29: unknown fast path enqueues no prompt"
    assert_output_contains "PASS: pg29: unknown fast path writes no audit row"
    assert_output_contains 'PASS: pg29: with the allow rule CheckPermission returns "allow"'
    assert_output_contains 'PASS: pg29: with a deny cache row CheckPermission returns "deny"'
}

# --- s122: cache, revoke, rate limit ----------------------------------------

@test "pg07-cli-roundtrip: qdistro-approvals list/audit/revoke/audit-gc against the broker" {
    run_case s122-perm-cache.sh pg07
    assert_output_contains "PASS: pg07: 'list' shows all three seeded actions"
    assert_output_contains "PASS: pg07: 'revoke <id>' exits 0 and reports it"
    assert_output_contains "PASS: pg07: revoke audited as source=revoke, approver_uid=0 (root CLI)"
    assert_output_contains "PASS: pg07: revoke of a missing id exits 1"
    assert_output_contains "PASS: pg07: audit-gc reports deleted N>=3 rows"
}

@test "pg23-revoke-all-for-uid: one ApprovalRevoked per row, other uid survives, 3 revoke audit rows" {
    run_case s122-perm-cache.sh pg23
    assert_output_contains "PASS: pg23: exactly three ApprovalRevoked signals"
    assert_output_contains "survive, no uid"
    assert_output_contains "PASS: pg23: three source=revoke audit rows"
}

@test "pg30-ratelimit-rejection: 50 ok, 51st .RateLimited, per-action bucket, recovers" {
    run_case s122-perm-cache.sh pg30
    assert_output_contains "PASS: pg30: exactly 50 CheckPermission calls answered 'unknown' before the limiter fired"
    assert_output_contains "PASS: pg30: the 51st call raised org.qdistro.AdminBroker1.RateLimited"
    assert_output_contains "PASS: pg30: a different action under the same uid is not limited"
    assert_output_contains "PASS: pg30: after the 1s window the original action answers again"
}

@test "pg31-fire-and-forget-request: queued >=10s with no waiter; approve -> RequestDecided + cache row" {
    run_case s122-perm-cache.sh pg31
    assert_output_contains "PASS: pg31: still pending after 10 s with no waiter"
    assert_output_contains 'PASS: pg31: one RequestDecided signal carrying'
    assert_output_contains "PASS: pg31: the cache row was written with no waiter (1h scope)"
}

@test "pg32-forever-exe-scope-isolation: forever_exe hits the same exe, re-prompts for another" {
    run_case s122-perm-cache.sh pg32
    assert_output_contains "PASS: pg32: cache row 2000|exe_only|<python exe>|forever_exe"
    assert_output_contains "PASS: pg32: second python request is a cache hit: ALLOWED"
    assert_output_contains "PASS: pg32: a different exe re-prompts"
    assert_output_contains "PASS: pg32: the other-exe caller sees the deny"
}

@test "pg33-cache-expiry-rungc: expired row not served; RunCacheGc deletes it" {
    run_case s122-perm-cache.sh pg33
    assert_output_contains 'PASS: pg33: the expired row is NOT served: "unknown"'
    assert_output_contains "PASS: pg33: RunCacheGc (admin) deleted the expired row"
    assert_output_contains "PASS: pg33: expired row gone from approvals"
}

# --- s123: clipboard / handoff gates, ListRules, SaveRule ---------------------

@test "pg36-clipboard-same-silo-allow: allow + clipboard_same_silo audit; deny rule ignored" {
    run_case s123-perm-gates.sh pg36
    assert_output_contains "PASS: pg36: same-silo CheckClipboardTransfer(user1,user1) returns allow"
    assert_output_contains "PASS: pg36: audit row decision 1, source clipboard_same_silo*"
    assert_output_contains "PASS: pg36: same-silo short-circuit ignores the deny rule: still allow"
}

@test "pg37-clipboard-cross-silo-default-deny: default deny, directional rule allow with rule_path" {
    run_case s123-perm-gates.sh pg37
    assert_output_contains "PASS: pg37: cross-silo with no rule: deny"
    assert_output_contains "PASS: pg37: audit decision 1, source clipboard_rule*, rule_path = the rule file"
    assert_output_contains "PASS: pg37: rules are directional: admin->user1 still deny"
}

@test "pg38-listrules-surface: dict shape, uid=-1/exe=\"\" sentinels, source_path; non-admin AccessDenied" {
    run_case s123-perm-gates.sh pg38
    assert_output_contains "PASS: pg38: ListRules returns exactly the 3 dicts"
    assert_output_contains "PASS: pg38: non-admin (work) ListRules is refused with AccessDenied"
}

@test "pg39-saverule-validation: traversal / bad YAML / bad shape refused, nothing written" {
    run_case s123-perm-gates.sh pg39
    assert_output_contains "PASS: pg39: traversal filename refused with RulesEngineRefused"
    assert_output_contains "PASS: pg39: invalid YAML refused with RulesEngineRefused"
    assert_output_contains "PASS: pg39: dict-not-list schema refused with RulesEngineRefused"
    assert_output_contains "PASS: pg39: ListRules count unchanged by the refused saves"
}

@test "pg40-clipboard-receive-same-silo: allow + same-silo audit; deny rule ignored" {
    run_case s123-perm-gates.sh pg40
    assert_output_contains "PASS: pg40: same-silo CheckClipboardReceive returns allow"
    assert_output_contains "PASS: pg40: audit decision 1, source contains clipboard_receive_same_silo"
    assert_output_contains "PASS: pg40: same-silo receive short-circuit ignores the deny rule"
}

@test "pg41-clipboard-receive-mime-glob: default deny, wildcard rule, text/* glob allow/deny + audit" {
    run_case s123-perm-gates.sh pg41
    assert_output_contains "PASS: pg41: text/* rule: text/html allow"
    assert_output_contains "PASS: pg41: text/* rule: image/png deny"
    assert_output_contains "PASS: pg41: text/* rule: application/pdf deny"
    assert_output_contains "PASS: pg41: audit: 2 rule allows"
}

@test "pg42-handoff-activation: same-silo allow, default deny, app_id rule allow/miss, non-admin denied" {
    run_case s123-perm-gates.sh pg42
    assert_output_contains "PASS: pg42: same-silo handoff: allow"
    assert_output_contains "PASS: pg42: app_id rule: firefox allow"
    assert_output_contains "PASS: pg42: app_id rule: chrome deny"
    assert_output_contains "PASS: pg42: audit: chrome default-deny, firefox rule allow with rule_path"
    assert_output_contains "PASS: pg42: non-admin (work) CheckHandoffActivation refused with AccessDenied"
}

# --- s124: lineage enforcement ------------------------------------------------

@test "pg58-lineage-enforce-forged-secctx: shadow allow, enforce unknown, RegisterLaunch root-only, attested allow" {
    run_case s124-perm-lineage.sh pg58
    assert_output_contains 'PASS: pg58: shadow mode trusts the forged sandbox_engine claim: "allow"'
    assert_output_contains 'PASS: pg58: enforce mode drops the forged claim of an unregistered caller: "unknown"'
    assert_output_contains "PASS: pg58: RegisterLaunch from the work uid is refused (AccessDenied)"
    assert_output_contains "PASS: pg58: registered caller with NO claim gets the attested engine: allow"
    assert_output_contains "PASS: pg58: RegisterLaunch wrote a qdistro.lineage.register:* audit row"
}

@test "pg59-lineage-cross-silo-source-pid: shadow allow; enforce no-pid/unregistered deny, attested allow, forged claim deny" {
    run_case s124-perm-lineage.sh pg59
    assert_output_contains "PASS: pg59: enforce: no source pid -> deny"
    assert_output_contains "PASS: pg59: enforce: unregistered source pid -> deny"
    assert_output_contains "PASS: pg59: enforce: registered source (attested work) -> allow"
    assert_output_contains "PASS: pg59: enforce: forged source claim 'work' overridden by attested 'scratch' -> deny"
    assert_output_contains "PASS: pg59: unattested-source denials left qdistro.lineage.source_deny:* audit rows"
}

# --- s125: cross-uid send-to --------------------------------------------------

@test "pg11-cross-user-sendto-headless: work->work2 stub notepad via relays, approve once, audit both uids, no cache" {
    run_case s125-sendto-xuser.sh pg11
    assert_output_contains "PASS: pg11: work2's notepad document contains"
    assert_output_contains "PASS: pg11: audit row 2000|app.send-to:3000:...|1|once|prompt|1000"
    assert_output_contains "PASS: pg11: one-shot send-to persisted no cache row"
}

@test "pg15-realapp-sendto-headless: two real qnotebooks, delivery both directions, audit, no cache" {
    run_case s125-sendto-xuser.sh pg15
    assert_output_contains "PASS: pg15: work2's qnotebook GetLastReceived"
    assert_output_contains "PASS: pg15: work's qnotebook GetLastReceived"
    assert_output_contains "PASS: pg15: audit rows for both directions"
    assert_output_contains "PASS: pg15: no send-to cache row was ever persisted"
}

@test "pg17-realapp-sendto-deny: deny -> sender .Denied, receiver unchanged, audit decision=0" {
    run_case s125-sendto-xuser.sh pg17
    assert_output_contains "PASS: pg17: positive control — approved sentinel delivered"
    assert_output_contains "PASS: pg17: the sender observes org.qdistro.AdminBroker1.Denied"
    assert_output_contains "PASS: pg17: work2's qnotebook never received the denied payload"
    assert_output_contains "PASS: pg17: this request's audit row is caller 2000, decision 0, source prompt"
}
