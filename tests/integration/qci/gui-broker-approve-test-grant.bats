#!/usr/bin/env bats
#
# Host-only tests for ci/lib/gates/gui.sh::grant_broker_approve_for_tests.
#
# Admin-uid approvals need the polkit action org.qdistro.broker.approve,
# which on a product system only a typed admin password creates. Agent-driven
# GUI scenarios cannot type one (and the CI admin session has no registered
# polkit agent), so the gate writes a TEST-ONLY polkit rule granting the
# action to admin into every scenario VM — except a scenario that opts out
# with `<!-- qci:polkit-test-grant: none -->` (permissions-gui/62, which tests
# the real password prompt), where it removes the rule instead.
#
# Like gui-idle-lock-suppression.bats, the vm-exec stub decodes the emitted
# base64 script and EXECUTES it, re-rooting /etc/polkit-1 into the test tree.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"

    TDIR="$BATS_TEST_TMPDIR"
    mkdir -p "$TDIR/bin" "$TDIR/tools" "$TDIR/root"
    PATH="$TDIR/bin:$PATH"
    VM_TOOLS="$TDIR/tools"
    RULE="$TDIR/root/etc/polkit-1/rules.d/10-qci-broker-approve-test.rules"

    cat > "$TDIR/tools/vm-exec" <<EOF
#!/usr/bin/env bash
b64=\$(awk -F"'" '{print \$4}' <<<"\$2")
if [ -z "\$b64" ] || ! printf '%s' "\$b64" | base64 -d > "$TDIR/decoded.sh" 2>/dev/null \
        || [ ! -s "$TDIR/decoded.sh" ]; then
    touch "$TDIR/STUB-BROKEN"; exit 90
fi
sed -i "s#/etc/polkit-1#$TDIR/root/etc/polkit-1#g" "$TDIR/decoded.sh"
PATH="$TDIR/bin:\$PATH" bash "$TDIR/decoded.sh"
EOF
    chmod +x "$TDIR/tools/vm-exec"
    cat > "$TDIR/bin/id" <<EOF
#!/usr/bin/env bash
printf '%s\n' "id \$*" >> "$TDIR/id.log"
exit 0
EOF
    chmod +x "$TDIR/bin/id"

    printf '# approve as admin\n' > "$TDIR/plain.md"
    printf '# real prompt\n<!-- qci:polkit-test-grant: none -->\n' > "$TDIR/optout.md"
}

@test "test grant: written for an ordinary scenario, admin-only, approve action only" {
    run grant_broker_approve_for_tests somevm "$TDIR/plain.md"
    [ "$status" -eq 0 ]
    [ ! -e "$TDIR/STUB-BROKEN" ]
    grep -q '^id admin$' "$TDIR/id.log"
    [ -f "$RULE" ]
    grep -qF 'action.id == "org.qdistro.broker.approve"' "$RULE"
    grep -qF 'subject.user == "admin"' "$RULE"
    grep -qF 'return polkit.Result.YES;' "$RULE"
    grep -q 'TEST-ONLY' "$RULE"
    # Exactly one grant, nothing broader.
    [ "$(grep -c 'polkit.Result' "$RULE")" -eq 1 ]
}

@test "test grant: an opted-out scenario gets it REMOVED, not written" {
    mkdir -p "$(dirname "$RULE")"
    echo stale > "$RULE"
    run grant_broker_approve_for_tests somevm "$TDIR/optout.md"
    [ "$status" -eq 0 ]
    [ ! -e "$TDIR/STUB-BROKEN" ]
    [ ! -e "$RULE" ]
}

@test "test grant: the rule path is the one image verification refuses" {
    grep -qF "check_absent \"[broker] no test grant of the approve action\" $QCI_BROKER_APPROVE_TEST_RULE" \
        "$REPO_ROOT/image/verify-contents.sh"
}

@test "test grant: every agent scenario VM gets it, first attempt and retries" {
    [ "$(grep -c 'grant_broker_approve_for_tests "\$vm" "\$scenario"' "$REPO_ROOT/ci/lib/gates/gui.sh")" -eq 1 ]
    [ "$(grep -c 'grant_broker_approve_for_tests "\$vmN" "\$scenario"' "$REPO_ROOT/ci/lib/gates/gui.sh")" -eq 1 ]
}

@test "test grant: the password-prompt scenario opts out" {
    local f="$REPO_ROOT/tests/integration/permissions-gui/62-admin-approval-password-prompt.md"
    run gui_scenario_wants_polkit_test_grant "$f"
    [ "$status" -ne 0 ]
    run gui_scenario_wants_polkit_test_grant \
        "$REPO_ROOT/tests/integration/permissions-gui/04-qt-admin-app-approve.md"
    [ "$status" -eq 0 ]
}
