#!/usr/bin/env bats
# qdistro-polkit-agent — logind session handling in a real guest.
#
# Regression for the agent crash-loop seen in every lingering guest journal:
#
#   qdistro-polkit-agent: registration failed:
#     org.freedesktop.PolicyKit1.Error.Failed: Cannot determine session the
#     caller is in
#   qdistro-polkit-agent.service: Scheduled restart job, restart counter is at 55.
#
# The agent is a SYSTEM service (User=admin) — its environment and cgroup
# must be root-owned so a same-uid process cannot inject code into the
# process the broker's relay trusts (sol r169). It starts at boot, before
# (or without) any login. polkitd only accepts a registration for the
# caller's session, and with only logind's class=manager session there is
# none. The agent must wait for a login instead of exiting, register once
# a login session appears, and still route a real authorization through
# itself to the broker.
load helpers

setup_file() {
    export VM_NAME VM_EXEC
}

teardown_file() {
    vm_run "systemctl stop qci-polkit-login.service qci-polkit-login2.service qci-polkit-subject.service qci-polkit-subject2.service 2>/dev/null; \
            rm -f /usr/share/polkit-1/actions/org.qdistro.test.agentsession.policy; true"
}

# vm_run merges vm-exec's own stderr into $output; when the identity probe
# answers before a fast guest command exits, the "[vm-exec] guest identity
# pinned" line lands in the capture and breaks exact-match assertions.
# Scalar reads go through this: guest output minus transport diagnostics.
_guest_value() {
    grep -v '^\[vm-exec\]' <<<"$output" | tail -1
}

_agent_restarts() {
    vm_run "systemctl show -p NRestarts --value qdistro-polkit-agent.service"
    output="$(_guest_value)"
}

@test "polkit-agent-session: waits without a login, registers on login, serves an authorization" {
    ensures "the admin polkit agent stays up without a login session and registers for the admin's login session when one appears"

    step "precondition: the agent unit is installed and admin has no login session"
    vm_run "test -f /etc/systemd/system/qdistro-polkit-agent.service"
    require "qdistro-polkit-agent.service installed in /etc/systemd/system"
    vm_run "loginctl list-sessions --no-legend -o json 2>/dev/null; echo; \
            for s in \$(loginctl list-sessions --no-legend | awk '\$3==\"admin\"{print \$1}'); do \
              loginctl show-session \"\$s\" -p Id -p Class -p Type -p State; done"
    echo "$output" >&2
    if grep -q '^Class=user$' <<<"$output"; then
        fail_loud "admin already has a class=user login session; the no-session case cannot be observed on this VM"
    fi

    step "with only the manager session, the agent stays active and does not restart"
    wait_for_unit qdistro-polkit-agent.service 30 \
        || fail_loud "qdistro-polkit-agent.service never went active"
    vm_run "sleep 10"
    _agent_restarts
    assert_eq_evidence "0" "$output" "agent NRestarts with no login session"
    vm_run "systemctl is-active qdistro-polkit-agent.service"
    assert_eq_evidence "active" "$(_guest_value)" "agent state with no login session"
    vm_run "journalctl -b --no-pager -o cat -t qdistro-polkit-agent"
    echo "$output" >&2
    assert_output_contains "no login session for uid 1000 yet; waiting"
    if grep -q "registration failed" <<<"$output"; then
        check_fail "no registration failures" \
            "$(grep 'registration failed' <<<"$output" | head -1)" \
            "the agent logs no registration failure while waiting"
        return 1
    fi
    check_pass "agent waits instead of failing" "NRestarts=0, waiting line logged"

    step "open a real logind login session for admin (PAM login stack on tty6)"
    vm_run "systemd-run --unit=qci-polkit-login -p PAMName=login -p User=admin \
            -p TTYPath=/dev/tty6 -p StandardInput=tty -p StandardOutput=journal \
            /usr/bin/sleep 900"
    assert_success
    wait_until_succeeds "loginctl list-sessions --no-legend | awk '\$3==\"admin\" && \$NF ~ /tty6/ {f=1} END{exit !f}' \
        || loginctl show-user admin -p Display --value | grep -q ." 30 \
        || fail_loud "the PAM login on tty6 did not create a logind session"
    vm_run "loginctl show-user admin -p Display --value"
    sid="$(_guest_value)"
    [[ -n "$sid" ]] || fail_loud "admin has no display session after the tty6 login"
    vm_run "loginctl show-session '$sid' -p Class --value"
    assert_eq_evidence "user" "$(_guest_value)" "class of admin's display session"

    step "the running agent registers for that session without a restart"
    wait_until_succeeds "journalctl -b --no-pager -o cat -t qdistro-polkit-agent | grep -F 'registered as session polkit agent (path=/org/qdistro/PolkitAgent, session=$sid)'" 30 \
        || fail_loud "agent did not register for login session $sid"
    _agent_restarts
    assert_eq_evidence "0" "$output" "agent NRestarts after registering"

    # The decision is DENY on purpose. An ALLOW cannot complete today, for a
    # reason outside this file's subject: polkitd accepts
    # AuthenticationAgentResponse/AuthenticationAgentResponse2 only from
    # uid 0 ("Only uid 0 may invoke this method"), and the agent runs as the
    # admin -- upstream agents answer through the setuid
    # polkit-agent-helper-1. What this test owns is that the authorization is
    # routed to THIS agent for THIS session and the round trip completes.
    step "an auth_admin check for a process in that session reaches the agent and the broker"
    vm_run "cat > /usr/share/polkit-1/actions/org.qdistro.test.agentsession.policy <<'POL'
<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE policyconfig PUBLIC \"-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN\"
 \"http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd\">
<policyconfig>
  <action id=\"org.qdistro.test.agentsession\">
    <description>qci polkit agent session probe</description>
    <message>qci polkit agent session probe</message>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_admin</allow_active>
    </defaults>
  </action>
</policyconfig>
POL"
    assert_success
    vm_run "systemctl show -p MainPID --value qci-polkit-login.service"
    subject_pid="$(_guest_value)"
    [[ "$subject_pid" =~ ^[1-9][0-9]*$ ]] || fail_loud "no subject pid in the login session (got '$subject_pid')"
    vm_run "systemd-run --unit=qci-polkit-subject -p StandardOutput=file:/run/qci-pkcheck.out \
            -p StandardError=file:/run/qci-pkcheck.out /bin/sh -c \
            'pkcheck --action-id org.qdistro.test.agentsession --process $subject_pid --allow-user-interaction; echo PKCHECK_RC=\$?'"
    assert_success
    wait_until_succeeds "runuser -u admin -- python3 -c 'import dbus; b=dbus.SystemBus(); o=b.get_object(\"org.qdistro.AdminBroker1\",\"/org/qdistro/AdminBroker1\"); [print(int(r[\"id\"]), r[\"action\"]) for r in o.GetPending(dbus_interface=\"org.qdistro.AdminBroker1\")]' | grep agentsession" 40 \
        || { vm_run "cat /run/qci-pkcheck.out; journalctl -b --no-pager -o cat -t qdistro-polkit-agent -u polkit | tail -20"; echo "$output" >&2; \
             fail_loud "no broker request for the probe action appeared"; }
    rid="$(awk '/agentsession/{print $1; exit}' <<<"$output")"
    check_pass "agent filed a broker request" "id=$rid ($output)"
    vm_run "runuser -u admin -- python3 -c 'import dbus; b=dbus.SystemBus(); o=b.get_object(\"org.qdistro.AdminBroker1\",\"/org/qdistro/AdminBroker1\"); o.DecideRequest($rid, \"deny\", \"once\", dbus_interface=\"org.qdistro.AdminBroker1\")'"
    assert_success
    wait_until_succeeds "grep -q PKCHECK_RC= /run/qci-pkcheck.out" 30 \
        || fail_loud "pkcheck did not complete after the broker decision"
    vm_run "cat /run/qci-pkcheck.out"
    echo "$output" >&2
    assert_output_contains "PKCHECK_RC=1"
    vm_run "journalctl -b --no-pager -o cat -t qdistro-polkit-agent"
    assert_output_contains "polkit BeginAuth: action=org.qdistro.test.agentsession method=broker"
    check_pass "the session's agent handled the authorization and the broker's denial ended it" \
        "BeginAuth logged; pkcheck PKCHECK_RC=1"

    step "a second, display-preferred login becomes the display session and the agent moves to it"
    sid_a="$sid"
    # logind elects the user's display session by type rank: a wayland
    # session outranks tty, so this PAM login takes Display while the tty6
    # session stays alive. An equal-rank tty login could never move the
    # display, and 'loginctl activate' only picks the seat's ACTIVE session —
    # it does not feed the user's display election.
    vm_run "systemd-run --unit=qci-polkit-login2 -p PAMName=login -p User=admin \
            -p Environment=XDG_SESSION_TYPE=wayland -p StandardOutput=journal \
            /usr/bin/sleep 900"
    assert_success
    # Discovery via structured session properties, polled: list-sessions
    # column order is not stable across systemd versions and the session
    # needs a moment to register with logind.
    wait_until_succeeds "for s in \$(loginctl list-sessions --no-legend | awk '\$3==\"admin\"{print \$1}'); do \
        [ \"\$(loginctl show-session \$s -p Type --value 2>/dev/null)\" = wayland ] && exit 0; done; exit 1" 30 \
        || fail_loud "no admin session of type wayland appeared"
    vm_run "for s in \$(loginctl list-sessions --no-legend | awk '\$3==\"admin\"{print \$1}'); do \
        [ \"\$(loginctl show-session \$s -p Type --value)\" = wayland ] && { echo \$s; break; }; done"
    sid_b="$(_guest_value | tr -d ' ')"
    [[ -n "$sid_b" && "$sid_b" != "$sid_a" ]] \
        || fail_loud "no second admin session of type wayland (got '$sid_b')"
    vm_run "loginctl show-session '$sid_b' -p Class --value"
    assert_eq_evidence "user" "$(_guest_value)" "class of the second session"
    wait_until_succeeds "loginctl show-user admin -p Display --value | grep -qx '$sid_b'" 30 \
        || { vm_run "loginctl show-user admin -p Display --value; loginctl show-session '$sid_b' -p Class -p Type -p Active -p State"; \
             echo "$output" >&2; \
             fail_loud "display session did not move to $sid_b"; }
    # The agent cannot UnregisterAuthenticationAgent for $sid_a any more —
    # polkitd accepts that call only for the caller's CURRENT session — so
    # the old registration is retracted by closing its private bus
    # connection and the session-B registration comes from a fresh one.
    wait_until_succeeds "journalctl -b --no-pager -o cat -t qdistro-polkit-agent | grep -F 'registered as session polkit agent (path=/org/qdistro/PolkitAgent, session=$sid_b)'" 45 \
        || { vm_run "journalctl -b --no-pager -o cat -t qdistro-polkit-agent | tail -20"; echo "$output" >&2; \
             fail_loud "agent did not register for the new display session $sid_b"; }
    wait_until_succeeds "journalctl -b --no-pager -o cat -t qdistro-polkit-agent | grep -F 'login session $sid_a ended'" 30 \
        || fail_loud "agent did not drop the registration for $sid_a"
    _agent_restarts
    assert_eq_evidence "0" "$output" "agent NRestarts after moving to $sid_b"
    check_pass "agent moved its registration to the new display session" \
        "registered session=$sid_b; session=$sid_a dropped"

    step "the second login ends; the agent must re-register the first session — the stale registration must not block it"
    # With a lingered stale entry polkitd refuses the second registration
    # for $sid_a as a duplicate; that is the astra r149 defect this case
    # reproduces. Removing B makes logind re-elect $sid_a as the display
    # session on its own — equal-rank survivor, no activate needed.
    vm_run "systemctl stop qci-polkit-login2.service"
    wait_until_succeeds "loginctl show-user admin -p Display --value | grep -qx '$sid_a'" 30 \
        || { vm_run "loginctl show-user admin -p Display --value"; echo "$output" >&2; \
             fail_loud "display session did not return to $sid_a"; }
    wait_until_succeeds "journalctl -b --no-pager -o cat -t qdistro-polkit-agent | grep -cF 'session=$sid_a)' | awk '\$1 >= 2 {f=1} END{exit !f}'" 45 \
        || { vm_run "journalctl -b --no-pager -o cat -t qdistro-polkit-agent | tail -20"; echo "$output" >&2; \
             fail_loud "agent never re-registered session $sid_a (stale registration left behind?)"; }
    vm_run "journalctl -b --no-pager -o cat -t qdistro-polkit-agent | grep -F 'registration for session'"
    if grep -qE 'already exists|is already registered' <<<"$output"; then
        check_fail "re-registration of $sid_a accepted" \
            "duplicate-agent refusal" \
            "polkitd kept the stale $sid_a registration and refused the new one: $output"
        return 1
    fi
    check_pass "re-registration for the returning session succeeded" \
        "second 'session=$sid_a' registration logged"

    step "the re-registered agent still serves authorizations for that session"
    # Fresh artifact: file: output does not truncate old content, so a stale
    # PKCHECK_RC line from the first authorization must not satisfy the wait.
    vm_run "rm -f /run/qci-pkcheck2.out"
    vm_run "systemd-run --unit=qci-polkit-subject2 -p StandardOutput=file:/run/qci-pkcheck2.out \
            -p StandardError=file:/run/qci-pkcheck2.out /bin/sh -c \
            'pkcheck --action-id org.qdistro.test.agentsession --process $subject_pid --allow-user-interaction; echo PKCHECK_RC=\$?'"
    assert_success
    wait_until_succeeds "runuser -u admin -- python3 -c 'import dbus; b=dbus.SystemBus(); o=b.get_object(\"org.qdistro.AdminBroker1\",\"/org/qdistro/AdminBroker1\"); [print(int(r[\"id\"]), r[\"action\"]) for r in o.GetPending(dbus_interface=\"org.qdistro.AdminBroker1\")]' | grep agentsession" 40 \
        || { vm_run "cat /run/qci-pkcheck2.out; journalctl -b --no-pager -o cat -t qdistro-polkit-agent -u polkit | tail -20"; echo "$output" >&2; \
             fail_loud "no broker request for the re-registered session"; }
    rid="$(awk '/agentsession/{print $1; exit}' <<<"$output")"
    vm_run "runuser -u admin -- python3 -c 'import dbus; b=dbus.SystemBus(); o=b.get_object(\"org.qdistro.AdminBroker1\",\"/org/qdistro/AdminBroker1\"); o.DecideRequest($rid, \"deny\", \"once\", dbus_interface=\"org.qdistro.AdminBroker1\")'"
    assert_success
    wait_until_succeeds "grep -q PKCHECK_RC= /run/qci-pkcheck2.out" 30 \
        || fail_loud "pkcheck did not complete after the broker decision"
    vm_run "cat /run/qci-pkcheck2.out"
    assert_output_contains "PKCHECK_RC=1"
    check_pass "re-registered session still routes to the agent" \
        "BeginAuth round trip after A->B->A; PKCHECK_RC=1"

    step "logout: the agent unregisters, keeps running, does not restart"
    # The 'ended or is no longer' line was already logged once when the
    # display moved from $sid_a to $sid_b; only a NEW line proves the final
    # logout was reconciled, so compare counts, not presence.
    vm_run "journalctl -b --no-pager -o cat -t qdistro-polkit-agent | grep -cF 'ended or is no longer'"
    drops_before="$(_guest_value)"
    vm_run "systemctl stop qci-polkit-login.service"
    wait_until_succeeds "journalctl -b --no-pager -o cat -t qdistro-polkit-agent | grep -cF 'ended or is no longer' | awk -v n='$drops_before' '\$1 > n {f=1} END{exit !f}'" 30 \
        || fail_loud "agent did not notice session $sid_a ending"
    _agent_restarts
    assert_eq_evidence "0" "$output" "agent NRestarts after logout"
    vm_run "systemctl is-active qdistro-polkit-agent.service"
    assert_eq_evidence "active" "$(_guest_value)" "agent state after logout"
}
