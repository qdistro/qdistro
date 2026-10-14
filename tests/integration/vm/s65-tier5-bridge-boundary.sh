#!/bin/bash
# s65-tier5-bridge-boundary.sh — GUEST driver (root) for
# tier5-bridge-enforcing.bats: the tier-5 privileged boundary (decision
# record step 8) on an SELinux-ENFORCING clone of the per-run golden.
#
#   spawn-tier5 (admin) ─ pkexec ─▶ qdistro-tier5-bridge (root)
#                                    └─ runuser (root) ─ qdistro-secctx-exec (admin) ─ waypipe (admin)
#
# polkit org.qdistro.tier5.bridge: allow_any=auth_admin, allow_inactive=no,
# allow_active=auth_admin_keep; the qdistro polkit agent forces the PAM route
# for it. These cases run with NO cached grant and WITHOUT the YES test rule
# (/etc/polkit-1/rules.d/10-qdistro-tier5-bridge-test.rules) the other tier-5
# drivers install. Usage:
#
#   s65-tier5-bridge-boundary.sh <case>
#
# Cases (one bats @test each): setup posture no-session inactive cancel
# wrong-password auth-success keep-same-caller admin-register admin-parent
# direct-invoke malformed env-injection pid-report exec-failure
# broker-failure caller-death postconditions teardown.
#
# Two CI-VM adaptations, the same ones permissions-gui/62 makes (read it):
#
#  1. A logind login session for the agent. CI VMs run the admin session under
#     a lingering user manager with no logind seat session, so `setup` opens a
#     real PAM `login` session for admin on tty6 (polkit-agent-session.bats)
#     and the agent registers for it. Callers run in transient scopes of
#     admin's user manager; polkitd resolves them to that display session.
#  2. That tty6 session is not the foreground VT, so the shipped
#     allow_inactive=no refuses it outright — the `inactive` case PROVES that
#     first. Every later case runs with a rule returning AUTH_ADMIN_KEEP for
#     admin on this action: the requirement allow_active states for an active
#     session. It grants nothing; `cancel` and `wrong-password` prove it.
#
# The password reaches the agent through its test seam
# QDISTRO_POLKIT_NONINTERACTIVE (password=<pw> | deny), set by a runtime
# drop-in on qdistro-polkit-agent.service. The seam replaces only the Qt
# prompt: the agent still runs the forced PAM route for this action and
# verifies the password with PAM as `admin`; the `allow` shortcut cannot
# apply to a forced-PAM action. The test-VM admin password is Pa_ssw0rd45
# (scripts/vm/fresh-vm-bootstrap.sh, build-baseweed-from-scratch.sh).
#
# Release-like posture: the per-run golden is a dev-profile install, which
# grants admin passwordless sudo (qdistro-bootstrap.sh, dev only). That rule
# makes every admin→root boundary moot, so `setup` moves
# /etc/sudoers.d/99-admin aside for the file's duration and `posture` proves
# `sudo -n` is refused. polkit temporary authorizations are kept per
# caller PROCESS (pid + start time; polkitd's subject_equal_for_authz): a
# grant obtained by one caller is reused only by that same caller, which
# `keep-same-caller` pins.
#
# Fault injection (pid-report, exec-failure, caller-death) bind-mounts a
# root-owned 0755 stand-in, labelled like the real file, over
# /usr/bin/qdistro-secctx-exec or /usr/bin/waypipe for one case — a root
# test harness simulating a broken or hostile reporter / an exec failure.
# Every case unmounts it again; `postconditions` re-hashes the real files.
#
# Output: one `PASS:`/`FAIL:` line per check, `[s65:<case>] N passes, M
# failures`, exit 1 on any failure. Precondition gaps print FAIL, never SKIP.
set -u

CASE=${1:?usage: $0 <case>}

W=/run/qci-t5b                 # root-owned test state (never product state)
U=$W/u                         # admin-owned work dir: caller outputs
S=$W/s                         # caller scripts (root-written, world-readable)
EVIL=$W/evil                   # env-injection payloads
FAKES=/usr/local/libexec/qci-t5b
BRIDGE=/usr/libexec/qdistro/qdistro-tier5-bridge
SPAWN=/usr/local/bin/qdistro-tier5-spawn
SECCTX=/usr/bin/qdistro-secctx-exec
WAYPIPE=/usr/bin/waypipe
ACTION=org.qdistro.tier5.bridge
AUTH_RULE=/etc/polkit-1/rules.d/11-qci-s65-tier5-bridge-auth.rules
TEST_YES_RULE=/etc/polkit-1/rules.d/10-qdistro-tier5-bridge-test.rules
LOG_RULE=/etc/polkit-1/rules.d/05-qci-s65-subject-log.rules
AGENT=qdistro-polkit-agent.service
DROPIN_DIR=/run/systemd/system/$AGENT.d
DROPIN=$DROPIN_DIR/90-qci-s65-seam.conf
LOGIN_UNIT=qci-s65-login
BROKER=qdistro-admin-broker.service
SUDOERS=/etc/sudoers.d/99-admin
SUDOERS_SAVED=/root/.qci-s65-99-admin
AUDIT_DB=/var/lib/qdistro/audit/audit.sqlite
PW=Pa_ssw0rd45
ADMIN_UID=1000
RT=/run/user/$ADMIN_UID

PASSN=0; FAILN=0
pass() { echo "PASS: $*"; PASSN=$((PASSN + 1)); }
fail() { echo "FAIL: $*"; FAILN=$((FAILN + 1)); }
info() { echo "  info: $*"; }
finish() {
    echo "[s65:$CASE] $PASSN passes, $FAILN failures"
    [ "$FAILN" -eq 0 ] && [ "$PASSN" -gt 0 ]
    exit $?
}
check() {   # check <name> <cmd...> — PASS when cmd succeeds
    local name=$1; shift
    if "$@"; then pass "$name"; else fail "$name"; fi
}

cursor() { journalctl -n0 --show-cursor 2>/dev/null | sed -n 's/^-- cursor: //p'; }
alog()   { journalctl -b --no-pager -o cat -t qdistro-polkit-agent ${1:+--after-cursor="$1"} 2>/dev/null; }
comp_log() { journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service -b --no-pager -o cat ${1:+--after-cursor="$1"} 2>/dev/null; }
pklog()  { journalctl -u polkit.service -b --no-pager -o cat ${1:+--after-cursor="$1"} 2>/dev/null; }
blog()   { journalctl -u "$BROKER" -b --no-pager -o cat ${1:+--after-cursor="$1"} 2>/dev/null; }
wait_for() {   # wait_for <secs> <cmd...>
    local end=$((SECONDS + $1)); shift
    while [ "$SECONDS" -lt "$end" ]; do "$@" && return 0; sleep 0.25; done
    "$@"
}
starttime() { local s; { read -r s < "/proc/$1/stat"; } 2>/dev/null || return 1; s="${s##*) }"; set -- $s; echo "${20}"; }
ppid_of() { awk '/^PPid:/ {print $2}' "/proc/$1/status" 2>/dev/null; }
uid_of()  { awk '/^Uid:/ {print $2}' "/proc/$1/status" 2>/dev/null; }
exe_of()  { readlink "/proc/$1/exe" 2>/dev/null; }
environ_of() { tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null; }
token() { od -An -N16 -tx1 /dev/urandom | tr -d ' \n'; }
newport() { echo $(( 20000 + (RANDOM % 20000) )); }
newloop() { echo "loopback-$(( 900000 + (RANDOM % 99999) ))"; }
audit_max_id() { sqlite3 "$AUDIT_DB" "SELECT coalesce(max(id),0) FROM audit;" 2>/dev/null || echo QUERY-FAILED; }
audit_rows_after() {   # audit_rows_after <id> <action-LIKE>
    sqlite3 "$AUDIT_DB" "SELECT count(*) FROM audit WHERE id > $1 AND action LIKE '$2';" 2>/dev/null || echo QUERY-FAILED
}
broker_up() { busctl --system list --no-pager 2>/dev/null | grep -q '^org\.qdistro\.AdminBroker1 '; }

# --- the launch chain, for "nothing launched" / "nothing survives" ----------
# One line per process belonging to a tier-5 bridge chain (or this file's
# fault-injection stand-ins): the bridge, its runuser, secctx-exec, waypipe,
# a pkexec still waiting, the stand-in's `sleep 601`.
chain_procs() {
    local p pid exe cmd
    for p in /proc/[0-9]*; do
        pid=${p#/proc/}
        exe=$(readlink "$p/exe" 2>/dev/null) || continue
        cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
        case "$cmd" in
            *"$BRIDGE"*|"sleep 601 ") ;;
            *)
                case "$exe" in
                    "$SECCTX"|"$WAYPIPE"|*/pkexec) ;;
                    */runuser) case "$cmd" in *qdistro-secctx-exec*) ;; *) continue ;; esac ;;
                    *) continue ;;
                esac ;;
        esac
        echo "$pid:$(starttime "$pid") uid=$(uid_of "$pid") exe=$exe cmd=$cmd"
    done
}
new_chain_procs() {
    chain_procs | while IFS= read -r l; do
        grep -qxF "${l%% *}" "$W/baseline-procs" 2>/dev/null || printf '%s\n' "$l"
    done
}
chain_gone() { [ -z "$(new_chain_procs)" ]; }
assert_chain_gone() {   # assert_chain_gone <label> [secs]
    if wait_for "${2:-15}" chain_gone; then
        pass "$1: no bridge/runuser/secctx-exec/waypipe/pkexec process survives"
    else
        fail "$1: surviving chain process(es): $(new_chain_procs | tr '\n' ';')"
    fi
}

# --- callers: admin, inside a transient scope of admin's user manager -------
# polkitd resolves such a process (user-1000.slice, no session scope) to the
# admin's display session; a process left in the root shell's cgroup would
# resolve to root's SSH session instead.
as_admin() {
    runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin USER=admin \
        LOGNAME=admin LANG=C.UTF-8 XDG_RUNTIME_DIR=$RT \
        DBUS_SESSION_BUS_ADDRESS=unix:path=$RT/bus "$@"
}

scope_run() {   # scope_run <tag> <script> — background; outputs land in $U/<tag>.*
    local tag=$1 script=$2
    rm -f "$U/$tag".*
    as_admin systemd-run --user --scope --quiet --collect \
        --unit="qci-s65-$tag-$RANDOM" -- /bin/bash "$script" \
        >"$U/$tag.scope.log" 2>&1 </dev/null &
}
# A caller script that runs the bridge through pkexec with a held stdin pipe
# (the bridge's caller-liveness channel), records the exit status, and
# releases the pipe. Args are the bridge argv, written %q-quoted.
bridge_script() {   # bridge_script <tag> <bridge args...>
    local tag=$1; shift
    {
        echo '#!/bin/bash'
        echo "exec 3< <(exec sleep 6$((RANDOM % 900 + 100)))"
        echo "echo \$! > $U/$tag.holder; echo \$\$ > $U/$tag.caller"
        printf '/usr/bin/pkexec %q' "$BRIDGE"; printf ' %q' "$@"
        echo " <&3 3<&- >$U/$tag.out 2>$U/$tag.err"
        echo "echo \$? > $U/$tag.rc"
        echo "kill \$(cat $U/$tag.holder) 2>/dev/null"
    } > "$S/$tag.sh"
    chmod 0644 "$S/$tag.sh"
}
rc_of() { cat "$U/$1.rc" 2>/dev/null; }
wait_rc() { wait_for "${2:-60}" test -s "$U/$1.rc"; }
kill_caller() {   # SIGKILL the caller shell and its stdin holder
    local p
    for p in "$(cat "$U/$1.holder" 2>/dev/null)" "$(cat "$U/$1.caller" 2>/dev/null)"; do
        [ -n "$p" ] && kill -KILL "$p" 2>/dev/null
    done
    return 0
}

# --- the agent's seam -------------------------------------------------------
agent_seam() {   # agent_seam password=<pw> | deny | none
    local c ul frag
    c=$(cursor)
    if [ "$1" = none ]; then
        rm -f "$DROPIN"
    else
        frag=$(systemctl show -P FragmentPath "$AGENT")
        ul=$(sed -n 's/^UnsetEnvironment=//p' "$frag" | tr ' ' '\n' \
             | grep -vx 'QDISTRO_POLKIT_NONINTERACTIVE' | grep . | tr '\n' ' ')
        install -d -m 0755 "$DROPIN_DIR"
        printf '%s\n' '# qci s65 (tier5-bridge-enforcing.bats) TEST-ONLY seam; removed by teardown.' \
            '[Service]' 'UnsetEnvironment=' "UnsetEnvironment=$ul" \
            "Environment=\"QDISTRO_POLKIT_NONINTERACTIVE=$1\"" > "$DROPIN"
    fi
    systemctl daemon-reload
    systemctl restart "$AGENT" || return 1
    wait_for 45 sh -c "journalctl -b --no-pager -o cat -t qdistro-polkit-agent --after-cursor='$c' | grep -q 'registered as session polkit agent'" || return 1
    local mp; mp=$(systemctl show -P MainPID "$AGENT")
    if [ "$1" = none ]; then
        ! environ_of "$mp" | grep -q '^QDISTRO_POLKIT_NONINTERACTIVE='
    else
        environ_of "$mp" | grep -qxF "QDISTRO_POLKIT_NONINTERACTIVE=$1"
    fi
}
no_temp_grant() {   # no cached grant for the action in the display session
    local out
    out=$(as_admin systemd-run --user --scope --quiet --collect pkcheck --list-temp 2>&1) || true
    printf '%s\n' "$out" | sed 's/^/    list-temp: /'
    ! printf '%s\n' "$out" | grep -q "$ACTION"
}
install_auth_rule() {
    install -d -m 0755 /etc/polkit-1/rules.d
    cat > "$AUTH_RULE" <<'RULE'
// qci s65 (tier5-bridge-enforcing.bats) ONLY: the CI login session is not the
// foreground VT, so demand admin authentication (what allow_active states)
// instead of the shipped allow_inactive=no. Never YES. Removed by teardown.
polkit.addRule(function(action, subject) {
    if (action.id == "org.qdistro.tier5.bridge" && subject.user == "admin") {
        return polkit.Result.AUTH_ADMIN_KEEP;
    }
});
RULE
    chmod 0644 "$AUTH_RULE"
    sleep 1   # polkitd reloads rules.d on change
}
no_registration_after() {   # no_registration_after <audit-id> <label>
    local n; n=$(audit_rows_after "$1" 'qdistro.lineage.register:%')
    if [ "$n" = 0 ]; then pass "$2: no launch record registered (audit rows=0)"
    else fail "$2: lineage.register rows after id $1: $n"; fi
}
no_tier5_commit_after() {   # no_tier5_commit_after <cursor> <label>
    local l; l=$(comp_log "$1" | grep 'qdwin/secctx: committed engine=qdistro.tier5 ' || true)
    if [ -z "$l" ]; then pass "$2: compositor saw no tier-5 secctx commit"
    else fail "$2: compositor committed a tier-5 context: $l"; fi
}
pkexec_refused() {   # pkexec_refused <tag> <label> — rc 127, bridge never ran
    local rc; rc=$(rc_of "$1")
    if [ "$rc" = 127 ] && ! grep -q '\[tier5-bridge\]' "$U/$1.err" 2>/dev/null \
            && ! [ -s "$U/$1.out" ]; then
        pass "$2: pkexec refused (rc=127: $(head -c 200 "$U/$1.err" | tr '\n' ' '))"
    else
        fail "$2: rc=$rc out=[$(cat "$U/$1.out" 2>/dev/null)] err=[$(head -c 400 "$U/$1.err" 2>/dev/null | tr '\n' ' ')]"
    fi
}

# --- fault injection ----------------------------------------------------------
bind_fake() {   # bind_fake <target> <fake-file>
    chcon --reference="$1" "$2" 2>/dev/null || true
    mount --bind "$2" "$1"
}
unbind_all() {
    local t
    for t in "$SECCTX" "$WAYPIPE"; do
        while findmnt -rn --target "$t" -o TARGET 2>/dev/null | grep -qxF "$t"; do
            umount "$t" 2>/dev/null || { umount -l "$t" 2>/dev/null; break; }
        done
    done
}
write_fake_secctx() {
    install -d -m 0755 "$FAKES"
    cat > "$FAKES/secctx-standin" <<'EOF'
#!/bin/bash
# qci s65 fault-injection stand-in for qdistro-secctx-exec: reports the pid
# named by /run/qci-t5b/fake-mode instead of its exec'd child.
fd=${QDISTRO_LAUNCH_REPORT_FD:-}
case "$(cat /run/qci-t5b/fake-mode 2>/dev/null)" in
    pid)     printf '%s\n' "$(cat /run/qci-t5b/fake-pid)" >&"$fd" ;;
    self)    printf '%s\n' "$$" >&"$fd" ;;
    garbage) printf '12ab\n' >&"$fd" ;;
    slow)    : ;;
esac
exec sleep 601
EOF
    chown root:root "$FAKES/secctx-standin"; chmod 0755 "$FAKES/secctx-standin"
    printf '#!/nonexistent/qci-s65-interpreter\n' > "$FAKES/waypipe-noexec"
    chown root:root "$FAKES/waypipe-noexec"; chmod 0755 "$FAKES/waypipe-noexec"
}

# =============================================================================
case "$CASE" in
setup)
    install -d -m 0755 "$W" "$S" "$EVIL"
    install -d -m 0700 -o admin -g "$(id -gn admin)" "$U"
    modprobe vsock_loopback 2>/dev/null || true
    # Release-like sudo posture for the file's duration (dev golden only).
    if [ -e "$SUDOERS" ] && [ ! -e "$SUDOERS_SAVED" ]; then
        mv -f "$SUDOERS" "$SUDOERS_SAVED"
    fi
    rm -f "$TEST_YES_RULE"
    # Log-only rule (returns nothing, so it never changes a verdict): records
    # which session/activity polkitd resolved each caller to, as evidence.
    install -d -m 0755 /etc/polkit-1/rules.d
    cat > "$LOG_RULE" <<'RULE'
// qci s65 (tier5-bridge-enforcing.bats) ONLY: evidence log, no verdict.
polkit.addRule(function(action, subject) {
    if (action.id == "org.qdistro.tier5.bridge") {
        polkit.log("s65-subject user=" + subject.user + " session=" + subject.session +
                   " local=" + subject.local + " active=" + subject.active);
    }
});
RULE
    chmod 0644 "$LOG_RULE"
    systemctl reset-failed "$LOGIN_UNIT" 2>/dev/null || true
    if ! systemctl is-active --quiet "$LOGIN_UNIT"; then
        for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '$3=="admin"{print $1}'); do
            if [ "$(loginctl show-session "$s" -p Class --value 2>/dev/null)" = user ]; then
                fail "admin already has a class=user session ($s): the controlled tty6 session cannot be established"
            fi
        done
        c=$(cursor)
        systemd-run --unit="$LOGIN_UNIT" -p PAMName=login -p User=admin \
            -p TTYPath=/dev/tty6 -p StandardInput=tty -p StandardOutput=journal \
            /usr/bin/sleep 7200 >/dev/null
        if wait_for 45 sh -c "journalctl -b --no-pager -o cat -t qdistro-polkit-agent --after-cursor='$c' | grep -q 'registered as session polkit agent'"; then
            pass "agent registered for admin's tty6 login session ($(loginctl show-user admin -p Display --value))"
        else
            fail "agent never registered for the tty6 login session: $(alog "$c" | tail -5 | tr '\n' ';')"
        fi
    fi
    write_fake_secctx
    unbind_all
    if [ ! -s "$W/sha" ]; then
        sha256sum "$SECCTX" "$WAYPIPE" "$BRIDGE" /usr/bin/dbus-send /usr/bin/pkexec > "$W/sha"
    fi
    if [ ! -e "$W/mark" ]; then
        chain_procs | awk '{print $1}' > "$W/baseline-procs"
        touch "$W/mark"
    fi
    pass "s65 setup complete (state in $W)"
    finish ;;

posture)
    m=$(getenforce 2>/dev/null)
    [ "$m" = Enforcing ] && pass "SELinux is Enforcing" || fail "SELinux mode is '$m', not Enforcing"
    info "profile: $(sed -n 's/^QDISTRO_PROFILE=//p' /etc/qdistro/profile 2>/dev/null | tail -1)"
    if as_admin sudo -n true >/dev/null 2>&1; then
        fail "admin still has passwordless sudo (boundary moot)"
    else
        pass "admin has no passwordless sudo (sudo -n refused)"
    fi
    st=$(stat -c '%u:%g %a' "$BRIDGE" 2>/dev/null)
    [ "$st" = "0:0 755" ] && pass "bridge installed root:root 0755 at $BRIDGE" || fail "bridge $BRIDGE: '$st'"
    head -n1 "$BRIDGE" | grep -qx '#!/usr/bin/python3 -I' \
        && pass "bridge starts an isolated interpreter (#!/usr/bin/python3 -I)" \
        || fail "bridge shebang: $(head -n1 "$BRIDGE")"
    pk=$(pkaction --action-id "$ACTION" --verbose 2>&1)
    printf '%s\n' "$pk" | sed 's/^/    pkaction: /'
    if printf '%s\n' "$pk" | grep -q 'implicit any: *auth_admin$' \
       && printf '%s\n' "$pk" | grep -q 'implicit inactive: *no$' \
       && printf '%s\n' "$pk" | grep -q 'implicit active: *auth_admin_keep$' \
       && printf '%s\n' "$pk" | grep -q "org.freedesktop.policykit.exec.path -> $BRIDGE"; then
        pass "shipped action defaults: any=auth_admin inactive=no active=auth_admin_keep, exec.path pinned"
    else
        fail "action $ACTION not installed as shipped"
    fi
    hits=$(grep -rl "$ACTION" /etc/polkit-1/rules.d /usr/share/polkit-1/rules.d 2>/dev/null | grep -vxF "$AUTH_RULE" | grep -vxF "$LOG_RULE" || true)
    [ -z "$hits" ] && pass "no polkit rule (YES test rule or other) names $ACTION" \
                   || fail "polkit rules naming $ACTION present: $hits"
    systemctl is-active --quiet "$AGENT" && pass "qdistro-polkit-agent active" || fail "qdistro-polkit-agent not active"
    forced=$(python3 -I - /usr/libexec/qdistro/qdistro_polkit_agent.py 2>&1 <<'EOF'
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "FORCED_PAM_ACTIONS" for t in node.targets):
        print("\n".join(sorted(c.value for c in ast.walk(node.value)
                               if isinstance(c, ast.Constant)
                               and isinstance(c.value, str))))
EOF
)
    if grep -qx "$ACTION" <<<"$forced"; then
        pass "installed agent forces the PAM route for $ACTION"
    else
        fail "installed agent does not list $ACTION in FORCED_PAM_ACTIONS"
    fi
    broker_up && pass "broker on the bus" || fail "broker not on the bus"
    qp=$(as_admin systemctl --user show -P MainPID qdwin-compositor.service 2>/dev/null)
    if [ -z "$qp" ] || [ "$qp" = 0 ]; then
        fail "no admin compositor process found"
    elif environ_of "$qp" | grep -q '^QDWIN_SECCTX_OPEN='; then
        fail "compositor runs with the dev-only QDWIN_SECCTX_OPEN"
    else
        pass "compositor (pid $qp) has no QDWIN_SECCTX_OPEN"
    fi
    runuser -u admin -- test -S "$RT/wayland-1" && pass "admin compositor socket up" || fail "no $RT/wayland-1"
    lsmod | grep -q '^vsock_loopback' && pass "vsock_loopback loaded" || fail "vsock_loopback not loaded"
    [ "$(loginctl show-session "$(loginctl show-user admin -p Display --value)" -p Active --value 2>/dev/null)" = no ] \
        && pass "admin's display session is the inactive tty6 login (adaptation 1)" \
        || fail "admin's display session is not the inactive tty6 login"
    if agent_seam none && no_temp_grant; then
        pass "agent has no seam and the session holds no cached grant for $ACTION"
    else
        fail "agent seam/cached-grant precondition"
    fi
    finish ;;

no-session)
    # A caller in root's SSH session cgroup (no admin session): auth_admin
    # with no agent for that session and no tty — refused, nothing launched.
    c=$(cursor); a=$(audit_max_id); L=$(newloop)
    out=$(runuser -u admin -- env -i PATH=/usr/bin:/bin XDG_RUNTIME_DIR=$RT \
          /usr/bin/pkexec "$BRIDGE" --loopback "$L" --port "$(newport)" --token "$(token)" </dev/null 2>&1)
    rc=$?
    [ "$rc" = 127 ] && pass "no-session: pkexec refused (rc=127: $(printf '%s' "$out" | head -c 200 | tr '\n' ' '))" \
                    || fail "no-session: rc=$rc out=$out"
    ! printf '%s' "$out" | grep -q '\[tier5-bridge\]' && pass "no-session: the bridge never ran" \
        || fail "no-session: bridge output seen: $out"
    alog "$c" | grep -q "BeginAuth: action=$ACTION" && fail "no-session: an agent was asked" \
        || pass "no-session: no agent authentication started"
    no_registration_after "$a" no-session
    assert_chain_gone no-session
    finish ;;

inactive)
    # Shipped allow_inactive=no, before the AUTH_ADMIN_KEEP adaptation exists.
    rm -f "$AUTH_RULE"; sleep 1
    agent_seam "password=$PW" || fail "inactive: agent seam not applied"
    c=$(cursor); a=$(audit_max_id); cc=$c
    bridge_script inactive --loopback "$(newloop)" --port "$(newport)" --token "$(token)"
    scope_run inactive "$S/inactive.sh"
    wait_rc inactive 60 || fail "inactive: caller never finished"
    pkexec_refused inactive "inactive session"
    disp=$(loginctl show-user admin -p Display --value)
    l=$(pklog "$c" | grep 's65-subject user=admin' | tail -1)
    if [[ "$l" == *"session=$disp "* && "$l" == *"active=false"* ]]; then
        pass "inactive: polkitd resolved the caller to the inactive display session $disp ($l)"
    else
        fail "inactive: polkitd subject not the inactive session $disp: '$l'"
    fi
    alog "$c" | grep -q "BeginAuth: action=$ACTION" \
        && fail "inactive: the agent was asked (allow_inactive=no must refuse without a prompt)" \
        || pass "inactive: refused without any authentication attempt (allow_inactive=no)"
    no_registration_after "$a" inactive
    no_tier5_commit_after "$cc" inactive
    assert_chain_gone inactive
    finish ;;

cancel)
    install_auth_rule
    agent_seam deny || fail "cancel: agent seam not applied"
    no_temp_grant && pass "cancel: no cached grant before the attempt" || fail "cancel: a cached grant exists"
    c=$(cursor); a=$(audit_max_id)
    bridge_script cancel --loopback "$(newloop)" --port "$(newport)" --token "$(token)"
    scope_run cancel "$S/cancel.sh"
    wait_rc cancel 60 || fail "cancel: caller never finished"
    pkexec_refused cancel "cancelled authentication"
    alog "$c" | grep -q "BeginAuth: action=$ACTION method=pam\$" \
        && pass "cancel: agent took the forced PAM route" || fail "cancel: no 'method=pam' BeginAuth: $(alog "$c" | tail -3 | tr '\n' ';')"
    alog "$c" | grep -q "action=$ACTION method=pam -> deny (prompt-cancelled)" \
        && pass "cancel: agent logged '-> deny (prompt-cancelled)'" || fail "cancel: no prompt-cancelled deny line"
    no_registration_after "$a" cancel
    no_temp_grant && pass "cancel: the cancelled attempt left no grant" || fail "cancel: grant after cancel"
    assert_chain_gone cancel
    finish ;;

wrong-password)
    install_auth_rule
    agent_seam "password=Wr0ng_${RANDOM}x" || fail "wrong-password: agent seam not applied"
    c=$(cursor); a=$(audit_max_id)
    bridge_script wrongpw --loopback "$(newloop)" --port "$(newport)" --token "$(token)"
    scope_run wrongpw "$S/wrongpw.sh"
    wait_rc wrongpw 60 || fail "wrong-password: caller never finished"
    pkexec_refused wrongpw "wrong password"
    l=$(alog "$c" | grep "action=$ACTION method=pam -> " || true)
    case "$l" in
        *"-> deny ("*) [[ "$l" == *"pam-ok"* || "$l" == *empty-password* || "$l" == *prompt-cancelled* ]] \
                           && fail "wrong-password: unexpected verdict line: $l" \
                           || pass "wrong-password: PAM rejected it ($l)" ;;
        *) fail "wrong-password: no PAM deny line: $(alog "$c" | tail -3 | tr '\n' ';')" ;;
    esac
    no_registration_after "$a" wrong-password
    faillock --user admin --reset 2>/dev/null || true
    assert_chain_gone wrong-password
    finish ;;

auth-success)
    # The real launcher (installed qdistro-tier5-spawn --loopback) with a
    # long-lived Wayland client, authenticated by the admin password via PAM.
    install_auth_rule
    agent_seam "password=$PW" || fail "auth-success: agent seam not applied"
    command -v qdistro-test-window >/dev/null || fail "auth-success: qdistro-test-window missing (bake regression)"
    c=$(cursor); a=$(audit_max_id)
    P=$(newport)
    printf '#!/bin/bash\ncd /\nexec %q --loopback -p %q -- qdistro-test-window --title s65-t5 >%q 2>%q\n' \
        "$SPAWN" "$P" "$U/succ.out" "$U/succ.err" > "$S/succ.sh"
    scope_run succ "$S/succ.sh"
    if wait_for 90 grep -q 'bridge ready: waypipe client pid=[0-9]* silo=' "$U/succ.err"; then
        pass "auth-success: spawn reported the bridge ready"
    else
        fail "auth-success: no 'bridge ready': $(tail -c 1500 "$U/succ.err" | tr '\n' ';')"
    fi
    alog "$c" | grep -q "action=$ACTION method=pam -> allow (pam-ok)" \
        && pass "auth-success: agent verified the password through PAM ('-> allow (pam-ok)')" \
        || fail "auth-success: no pam-ok allow line: $(alog "$c" | tail -3 | tr '\n' ';')"
    line=$(grep -m1 'bridge ready: waypipe client pid=' "$U/succ.err")
    WP=$(sed -n 's/.*pid=\([0-9]*\) silo=.*/\1/p' <<<"$line")
    SILO=$(sed -n 's/.* silo=\([^ ]*\).*/\1/p' <<<"$line")
    NAME=${SILO#vm-}
    TOK=$(sed -n 's/^LAUNCH_TOKEN=//p' "$U/succ.out" | head -1)
    info "waypipe=$WP silo=$SILO token=$TOK"
    if [ -n "$WP" ] && [ -d "/proc/$WP" ]; then
        SP=$(ppid_of "$WP"); RP=$(ppid_of "$SP"); BP=$(ppid_of "$RP"); CP=$(ppid_of "$BP")
        is_chain=1
        [ "$(exe_of "$WP")" = "$WAYPIPE" ] && [ "$(uid_of "$WP")" = "$ADMIN_UID" ] || is_chain=0
        [ "$(exe_of "$SP")" = "$SECCTX" ] && [ "$(uid_of "$SP")" = "$ADMIN_UID" ] || is_chain=0
        [ "$(uid_of "$RP")" = 0 ] && [ "$(cat "/proc/$RP/comm")" = runuser ] || is_chain=0
        [ "$(uid_of "$BP")" = 0 ] && tr '\0' ' ' < "/proc/$BP/cmdline" | grep -q "^/usr/bin/python3 -I $BRIDGE --loopback $NAME " || is_chain=0
        [ "$(uid_of "$CP")" = "$ADMIN_UID" ] || is_chain=0
        [ "$is_chain" = 1 ] \
            && pass "auth-success: chain admin spawn($CP) → root bridge($BP) → root runuser($RP) → admin secctx-exec($SP) → admin waypipe($WP)" \
            || fail "auth-success: unexpected chain: $(for p in "$WP" "$SP" "$RP" "$BP" "$CP"; do echo "$p:uid=$(uid_of "$p"):$(exe_of "$p")"; done | tr '\n' ' ')"
    else
        fail "auth-success: waypipe client pid '$WP' not live"
    fi
    if wait_for 20 sh -c "journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service -b --no-pager -o cat --after-cursor='$c' | grep -q 'qdwin/secctx: client accepted engine=qdistro.tier5 app_id=qdistro.tier5.$NAME instance_id=$TOK '"; then
        pass "auth-success: compositor tagged the client engine=qdistro.tier5 app_id=qdistro.tier5.$NAME instance_id=<launch token>"
    else
        fail "auth-success: no tagged client for qdistro.tier5.$NAME: $(comp_log "$c" | grep 'qdwin/secctx' | tail -3 | tr '\n' ';')"
    fi
    row=$(sqlite3 "$AUDIT_DB" "SELECT source FROM audit WHERE id > $a AND action='qdistro.lineage.register:$SILO' ORDER BY id DESC LIMIT 1;" 2>/dev/null)
    if [ "$SILO" = "vm-$NAME" ] && grep -q "pid=$WP starttime=$(starttime "$WP") uid=$ADMIN_UID engine='qdistro.tier5' app='qdistro.tier5.$NAME'" <<<"$row"; then
        pass "auth-success: broker registered pid=$WP (starttime-anchored) as silo $SILO, engine qdistro.tier5"
    else
        fail "auth-success: register row for $SILO: '$row'"
    fi
    # Close the app's session: the bridge must take its whole chain down.
    # SIGTERM to the admin wrapper (what closing the app does): its trap
    # closes the bridge's stdin.
    kill -TERM "$CP" 2>/dev/null
    wait_for 20 sh -c "! kill -0 $CP 2>/dev/null"
    pkill -u admin -f 'qdistro-test-window --title s65-t5' 2>/dev/null || true
    assert_chain_gone auth-success 20
    finish ;;

keep-same-caller)
    # One caller process, two pkexec runs: the first authenticates (PAM via
    # the seam), the seam then switches to deny, and the second must succeed
    # without a new authentication — the auth_admin_keep grant is bound to
    # this caller process.
    install_auth_rule
    agent_seam "password=$PW" || fail "keep: agent seam not applied"
    c=$(cursor)
    T1=$(token); T2=$(token); L1=$(newloop); L2=$(newloop)
    cat > "$S/keep.sh" <<EOF
#!/bin/bash
/usr/bin/pkexec $BRIDGE --loopback $L1 --port 1 --token $T1 </dev/null >$U/keep1.out 2>$U/keep1.err
echo \$? > $U/keep1.rc
while [ ! -e $U/keep.go ]; do sleep 0.2; done
exec 3< <(exec sleep 6999)
echo \$! > $U/keep2.holder
/usr/bin/pkexec $BRIDGE --loopback $L2 --port $(newport) --token $T2 <&3 3<&- >$U/keep2.out 2>$U/keep2.err
echo \$? > $U/keep2.rc
EOF
    rm -f "$U/keep.go"
    scope_run keep "$S/keep.sh"
    wait_rc keep1 60 || fail "keep: first pkexec never finished"
    [ "$(rc_of keep1)" = 2 ] && grep -q '\[tier5-bridge\] refused: port' "$U/keep1.err" \
        && pass "keep: first run authenticated and reached the bridge (it refused port 1)" \
        || fail "keep: first run rc=$(rc_of keep1) err=$(cat "$U/keep1.err")"
    alog "$c" | grep -q "action=$ACTION method=pam -> allow (pam-ok)" \
        && pass "keep: first run authenticated by PAM" || fail "keep: no pam-ok for the first run"
    agent_seam deny || fail "keep: deny seam not applied"
    c2=$(cursor)
    touch "$U/keep.go"
    if wait_for 60 grep -q '^READY [0-9]*$' "$U/keep2.out"; then
        pass "keep: second run from the same caller launched (READY $(sed 's/READY //' "$U/keep2.out"))"
    else
        fail "keep: second run not READY: rc=$(rc_of keep2) err=$(cat "$U/keep2.err" 2>/dev/null)"
    fi
    alog "$c2" | grep -q "BeginAuth: action=$ACTION" \
        && fail "keep: the second run started a new authentication" \
        || pass "keep: no new authentication for the second run (kept grant)"
    kill "$(cat "$U/keep2.holder" 2>/dev/null)" 2>/dev/null
    wait_rc keep2 30
    assert_chain_gone keep
    agent_seam "password=$PW" >/dev/null || true
    finish ;;

admin-register)
    a=$(audit_max_id)
    runuser -u admin -- sleep 300 & rp=$!
    wait_for 5 sh -c "pgrep -P $rp -x sleep >/dev/null"
    AP=$(pgrep -P "$rp" -x sleep | head -1)
    out=$(as_admin dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.RegisterLaunch \
        string:vm-s65forge string:qdistro.tier5 string:qdistro.tier5.s65forge \
        "string:$(token)" string:/usr/bin/sleep "uint64:${AP:-0}" string:tier5 \
        "uint64:$(starttime "${AP:-1}")" 2>&1); rc=$?
    if [ "$rc" != 0 ] && grep -q 'AccessDenied' <<<"$out"; then
        pass "admin-register: admin RegisterLaunch refused ($(head -c 200 <<<"$out" | tr '\n' ' '))"
    else
        fail "admin-register: rc=$rc out=$out"
    fi
    n=$(audit_rows_after "$a" 'qdistro.lineage.register:vm-s65forge')
    [ "$n" = 0 ] && pass "admin-register: no record for vm-s65forge" || fail "admin-register: rows=$n"
    kill "$rp" "$AP" 2>/dev/null
    finish ;;

admin-parent)
    c=$(cursor)
    # (a) the trusted-launcher marker under an admin parent (bash).
    out=$(as_admin bash -c "QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 WAYLAND_DISPLAY=wayland-1 \
        $SECCTX --sandbox-engine qdistro.tier5 --app-id qdistro.tier5.s65forge \
        --instance-id $(token) -- /usr/bin/true; echo rc=\$?" 2>&1)
    if grep -q 'refused: trusted marker is not backed by a direct root launcher parent' <<<"$out" \
       && ! grep -qx 'rc=0' <<<"$out"; then
        pass "admin-parent: secctx-exec refuses the marker under an admin parent"
    else
        fail "admin-parent: secctx-exec under admin parent: $out"
    fi
    # (b) the dev-only override: the compositor must still refuse the helper.
    out=$(as_admin bash -c "QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED=1 QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 \
        WAYLAND_DISPLAY=wayland-1 $SECCTX --sandbox-engine qdistro.tier5 \
        --app-id qdistro.tier5.s65forge2 --instance-id $(token) -- /usr/bin/true; echo rc=\$?" 2>&1)
    info "override run: $(tr '\n' ' ' <<<"$out" | head -c 300)"
    grep -qx 'rc=0' <<<"$out" && fail "admin-parent: override run exited 0" \
        || pass "admin-parent: dev-override run failed ($(grep -o 'rc=[0-9]*' <<<"$out"))"
    if comp_log "$c" | grep -q 'committed engine=qdistro.tier5 app_id=qdistro.tier5.s65forge'; then
        fail "admin-parent: compositor committed a forged tier-5 context"
    else
        pass "admin-parent: compositor committed no context for the admin-parent helpers"
    fi
    finish ;;

direct-invoke)
    out=$(as_admin "$BRIDGE" --loopback "$(newloop)" --port "$(newport)" --token "$(token)" </dev/null 2>&1); rc=$?
    [ "$rc" = 2 ] && grep -q 'refused: must run as root via pkexec' <<<"$out" \
        && pass "direct-invoke: admin exec of the bridge refused (rc=2)" || fail "direct-invoke admin: rc=$rc $out"
    out=$(env -i PATH=/usr/bin:/bin PKEXEC_UID=0 "$BRIDGE" --loopback "$(newloop)" --port "$(newport)" --token "$(token)" </dev/null 2>&1); rc=$?
    [ "$rc" = 2 ] && grep -q 'refused: only admin may invoke' <<<"$out" \
        && pass "direct-invoke: a non-admin PKEXEC_UID is refused (rc=2)" || fail "direct-invoke PKEXEC_UID=0: rc=$rc $out"
    assert_chain_gone direct-invoke
    finish ;;

malformed)
    install_auth_rule
    agent_seam "password=$PW" || fail "malformed: agent seam not applied"
    c=$(cursor); a=$(audit_max_id)
    T=$(token); L=$(newloop)
    long=$(printf 'a%.0s' $(seq 1 64))
    {
        echo '#!/bin/bash'
        echo 'i=0'
        echo "run() { i=\$((i+1)); /usr/bin/pkexec $BRIDGE \"\$@\" </dev/null >$U/mal.\$i.out 2>$U/mal.\$i.err; echo \$? > $U/mal.\$i.rc; }"
        for args in \
            "" \
            "--loopback $L --port 7777" \
            "--vm bad/name --port 7777 --token $T" \
            "--vm -rf --port 7777 --token $T" \
            "--vm loopback-5 --port 7777 --token $T" \
            "--vm $long --port 7777 --token $T" \
            "--loopback evil-1 --port 7777 --token $T" \
            "--loopback $L --port 80 --token $T" \
            "--loopback $L --port 70000 --token $T" \
            "--loopback $L --port 7777;id --token $T" \
            "--loopback $L --port 7777 --token ${T^^}" \
            "--loopback $L --port 7777 --token deadbeef" \
            "--loopback $L --port 7777 --token $T --cid 1" \
            "--exec /bin/sh --port 7777 --token $T"; do
            printf 'run'; for w in $args; do printf ' %q' "$w"; done; echo
        done
        printf 'run --loopback %q --port 7777 --token %q\n' "$L" "$T"$'\n'
        echo "echo \$i > $U/mal.count"
    } > "$S/mal.sh"
    scope_run mal "$S/mal.sh"
    wait_for 120 test -s "$U/mal.count" || fail "malformed: caller never finished"
    n=$(cat "$U/mal.count" 2>/dev/null || echo 0); bad=0
    for i in $(seq 1 "$n"); do
        rc=$(cat "$U/mal.$i.rc" 2>/dev/null)
        if [ "$rc" != 2 ] || ! grep -q '^\[tier5-bridge\] refused:' "$U/mal.$i.err" || [ -s "$U/mal.$i.out" ]; then
            bad=$((bad + 1)); info "argv #$i: rc=$rc err=$(cat "$U/mal.$i.err" 2>/dev/null | tr '\n' ' ')"
        fi
    done
    [ "$n" = 15 ] && [ "$bad" = 0 ] && pass "malformed: all $n malformed argv sets refused by the bridge (rc=2, 'refused:', no READY)" \
        || fail "malformed: $bad of $n argv sets not refused as expected"
    no_registration_after "$a" malformed
    no_tier5_commit_after "$c" malformed
    assert_chain_gone malformed
    finish ;;

env-injection)
    install_auth_rule
    agent_seam "password=$PW" || fail "env-injection: agent seam not applied"
    rm -f "$U"/evil-ran-*
    for t in waypipe runuser env dbus-send qdistro-secctx-exec python3 python3.13 sh bash true; do
        printf '#!/bin/sh\n: > %q/evil-ran-$(basename "$0")\nexit 99\n' "$U" > "$EVIL/$t"
        chmod 0755 "$EVIL/$t"
    done
    printf 'open(%r + "/evil-ran-python-site", "w").close()\n' "$U" > "$EVIL/sitecustomize.py"
    cp "$EVIL/sitecustomize.py" "$EVIL/usercustomize.py"
    cp "$EVIL/sitecustomize.py" "$EVIL/startup.py"
    printf ': > %q/evil-ran-bash-env\n' "$U" > "$EVIL/bashenv"
    chmod 0644 "$EVIL"/*.py "$EVIL/bashenv"
    c=$(cursor); L=$(newloop); T=$(token); P=$(newport)
    {
        echo '#!/bin/bash'
        echo "export PATH=$EVIL:/usr/bin:/bin PYTHONPATH=$EVIL PYTHONHOME=$EVIL PYTHONSTARTUP=$EVIL/startup.py"
        echo "export PYTHONINSPECT=1 PYTHONUSERBASE=$EVIL BASH_ENV=$EVIL/bashenv ENV=$EVIL/bashenv"
        echo "export LD_LIBRARY_PATH=$EVIL GCONV_PATH=$EVIL HOME=$EVIL XDG_RUNTIME_DIR=$EVIL WAYLAND_DISPLAY=evil-0"
        echo "export QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED=1 QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1"
        echo "export QDISTRO_LAUNCH_REPORT_FD=0 QDISTRO_LAUNCH_RECORD_PATH=$U/rec QDISTRO_LAUNCH_RECORD_TOKEN=x"
        echo "cd $EVIL"
        echo "exec 3< <(exec /usr/bin/sleep 6998)"
        echo "echo \$! > $U/env.holder"
        echo "/usr/bin/pkexec $BRIDGE --loopback $L --port $P --token $T <&3 3<&- >$U/env.out 2>$U/env.err"
        echo "echo \$? > $U/env.rc"
    } > "$S/env.sh"
    scope_run env "$S/env.sh"
    if wait_for 60 grep -q '^READY [0-9]*$' "$U/env.out"; then
        pass "env-injection: the bridge launched despite the hostile caller environment"
    else
        fail "env-injection: no READY: rc=$(rc_of env) err=$(cat "$U/env.err" 2>/dev/null | tr '\n' ' ')"
    fi
    WP=$(sed -n 's/^READY //p' "$U/env.out"); SP=$(ppid_of "$WP"); RP=$(ppid_of "$SP"); BP=$(ppid_of "$RP")
    ran=$(ls "$U"/evil-ran-* 2>/dev/null | tr '\n' ' ')
    [ -z "$ran" ] && pass "env-injection: no caller-planted executable, site hook or BASH_ENV ran" \
                  || fail "env-injection: planted code ran: $ran"
    want="HOME=/home/admin LANG=C.UTF-8 LOGNAME=admin PATH=/usr/bin:/bin USER=admin XDG_RUNTIME_DIR=$RT"
    got=$(environ_of "$WP" | grep -v '^WAYLAND_DISPLAY=' | sort | tr '\n' ' ' | sed 's/ $//')
    wd=$(environ_of "$WP" | sed -n 's/^WAYLAND_DISPLAY=//p')
    if [ "$got" = "$want" ] && [[ "$wd" == wayland-secctx-* ]] && [ -S "$RT/$wd" ]; then
        pass "env-injection: waypipe environment is exactly the bridge's ($want WAYLAND_DISPLAY=$wd)"
    else
        fail "env-injection: waypipe environment: [$got] WAYLAND_DISPLAY=$wd"
    fi
    se=$(environ_of "$SP" | sort | tr '\n' ' ')
    if grep -q 'QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1' <<<"$se" && grep -q 'WAYLAND_DISPLAY=wayland-1 ' <<<"$se" \
       && ! grep -qE "ALLOW_UNTRUSTED|LAUNCH_RECORD|LD_|PYTHON|$EVIL" <<<"$se"; then
        pass "env-injection: secctx-exec got only the constructed environment"
    else
        fail "env-injection: secctx-exec environment: $se"
    fi
    be=$(environ_of "$BP" | tr '\n' ' ')
    if ! grep -qE "(^| )(LD_|PYTHON|BASH_ENV|ENV=|GCONV_PATH|QDISTRO_)|$EVIL" <<<"$be" \
       && [ "$(readlink "/proc/$BP/cwd")" = / ] && [ "$(readlink "/proc/$WP/cwd")" = / ]; then
        pass "env-injection: bridge environment carries no caller variable; bridge and waypipe cwd=/"
    else
        fail "env-injection: bridge env [$be] cwd=$(readlink "/proc/$BP/cwd") waypipe cwd=$(readlink "/proc/$WP/cwd")"
    fi
    [ "$(exe_of "$WP")" = "$WAYPIPE" ] && [ "$(exe_of "$SP")" = "$SECCTX" ] \
        && pass "env-injection: the fixed $SECCTX and $WAYPIPE ran" || fail "env-injection: exes $(exe_of "$SP") $(exe_of "$WP")"
    pipes() { local f; for f in /proc/"$1"/fd/*; do readlink "$f"; done 2>/dev/null | grep '^pipe:' | sort -u; }
    shared=$(comm -12 <(pipes "$BP") <(pipes "$WP"))
    if [ -z "$shared" ] && ! environ_of "$WP" | grep -q '^QDISTRO_'; then
        pass "env-injection: waypipe shares no pipe with the root bridge and carries no QDISTRO_ variable"
    else
        fail "env-injection: waypipe shares bridge pipe(s) [$shared] or QDISTRO_ env"
    fi
    kill "$(cat "$U/env.holder" 2>/dev/null)" 2>/dev/null
    wait_rc env 30
    assert_chain_gone env-injection
    finish ;;

pid-report)
    install_auth_rule
    agent_seam "password=$PW" || fail "pid-report: agent seam not applied"
    write_fake_secctx
    runuser -u admin -- sleep 900 & frp=$!
    wait_for 5 sh -c "pgrep -P $frp -x sleep >/dev/null"
    FP=$(pgrep -P "$frp" -x sleep | head -1); FST=$(starttime "$FP")
    # a pid that existed and is gone: a short-lived admin process
    runuser -u admin -- sh -c 'echo $$ > /run/qci-t5b/u/stale.pid'
    STALE=$(cat "$U/stale.pid")
    bind_fake "$SECCTX" "$FAKES/secctx-standin" || fail "pid-report: cannot bind the stand-in"
    a=$(audit_max_id)
    for mode in foreign stale pid1 self garbage; do
        case $mode in
            foreign) echo pid > "$W/fake-mode"; echo "$FP" > "$W/fake-pid"; want_rc=5; want='is not in this bridge.s launch chain' ;;
            stale)   echo pid > "$W/fake-mode"; echo "$STALE" > "$W/fake-pid"; want_rc=5; want='is not live|not in this bridge.s launch chain' ;;
            pid1)    echo pid > "$W/fake-mode"; echo 1 > "$W/fake-pid"; want_rc=5; want='vanished|not in this bridge.s launch chain' ;;
            self)    echo self > "$W/fake-mode"; want_rc=5; want='is not in this bridge.s launch chain' ;;
            garbage) echo garbage > "$W/fake-mode"; want_rc=4; want='reported no launch' ;;
        esac
        chmod 0644 "$W/fake-mode" "$W/fake-pid" 2>/dev/null
        bridge_script "pr-$mode" --loopback "$(newloop)" --port "$(newport)" --token "$(token)"
        scope_run "pr-$mode" "$S/pr-$mode.sh"
        wait_rc "pr-$mode" 60 || fail "pid-report $mode: caller never finished"
        rc=$(rc_of "pr-$mode")
        if [ "$rc" = "$want_rc" ] && grep -qE "FAIL: .*($want)" "$U/pr-$mode.err" && ! [ -s "$U/pr-$mode.out" ]; then
            pass "pid-report $mode: refused (rc=$rc: $(grep -m1 FAIL "$U/pr-$mode.err"))"
        else
            fail "pid-report $mode: rc=$rc out=[$(cat "$U/pr-$mode.out")] err=[$(tr '\n' ' ' < "$U/pr-$mode.err")]"
        fi
        assert_chain_gone "pid-report $mode"
    done
    unbind_all
    no_registration_after "$a" pid-report
    if [ "$(starttime "$FP")" = "$FST" ]; then
        pass "pid-report: the foreign admin process ($FP) was not signalled"
    else
        fail "pid-report: foreign process $FP is gone or changed"
    fi
    kill "$FP" "$frp" 2>/dev/null
    finish ;;

exec-failure)
    install_auth_rule
    agent_seam "password=$PW" || fail "exec-failure: agent seam not applied"
    write_fake_secctx
    before=$(find "$RT" -maxdepth 1 -name 'wayland-secctx-*' -printf '%f\n' | sort)
    bind_fake "$WAYPIPE" "$FAKES/waypipe-noexec" || fail "exec-failure: cannot bind the unexecutable waypipe"
    a=$(audit_max_id); c=$(cursor)
    bridge_script execfail --loopback "$(newloop)" --port "$(newport)" --token "$(token)"
    scope_run execfail "$S/execfail.sh"
    wait_rc execfail 60 || fail "exec-failure: caller never finished"
    unbind_all
    rc=$(rc_of execfail)
    if [ "$rc" = 4 ] && grep -q 'secctx-exec reported no launch (exec failed?)' "$U/execfail.err" && ! [ -s "$U/execfail.out" ]; then
        pass "exec-failure: bridge saw no launch report and failed (rc=4)"
    else
        fail "exec-failure: rc=$rc err=[$(tr '\n' ' ' < "$U/execfail.err")]"
    fi
    grep -q 'execvp(/usr/bin/waypipe)' "$U/execfail.err" \
        && pass "exec-failure: the real secctx-exec reported the failed execvp" \
        || fail "exec-failure: no execvp failure from secctx-exec"
    no_registration_after "$a" exec-failure
    after=$(find "$RT" -maxdepth 1 -name 'wayland-secctx-*' -printf '%f\n' | sort)
    [ "$before" = "$after" ] && pass "exec-failure: no secctx listener socket left behind" \
        || fail "exec-failure: listener sockets changed: $(comm -13 <(echo "$before") <(echo "$after") | tr '\n' ' ')"
    assert_chain_gone exec-failure
    finish ;;

broker-failure)
    install_auth_rule
    agent_seam "password=$PW" || fail "broker-failure: agent seam not applied"
    L=$(newloop); T=$(token); P=$(newport); a=$(audit_max_id)
    cat > "$S/bf.sh" <<EOF
#!/bin/bash
/usr/bin/pkexec $BRIDGE --loopback $L --port 1 --token $T </dev/null >$U/bf1.out 2>$U/bf1.err
echo \$? > $U/bf1.rc
while [ ! -e $U/bf.go ]; do sleep 0.2; done
exec 3< <(exec sleep 6997)
echo \$! > $U/bf2.holder
/usr/bin/pkexec $BRIDGE --loopback $L --port $P --token $T <&3 3<&- >$U/bf2.out 2>$U/bf2.err
echo \$? > $U/bf2.rc
kill \$(cat $U/bf2.holder) 2>/dev/null
EOF
    rm -f "$U/bf.go"
    scope_run bf "$S/bf.sh"
    wait_rc bf1 60 || fail "broker-failure: authenticating run never finished"
    [ "$(rc_of bf1)" = 2 ] && pass "broker-failure: caller authenticated (bridge refused port 1)" \
        || fail "broker-failure: first run rc=$(rc_of bf1) $(cat "$U/bf1.err")"
    systemctl mask --runtime --now "$BROKER" >/dev/null 2>&1
    wait_for 20 sh -c '! busctl --system list --no-pager 2>/dev/null | grep -q "^org\.qdistro\.AdminBroker1 "' \
        && pass "broker-failure: broker stopped and masked" || fail "broker-failure: broker still on the bus"
    touch "$U/bf.go"
    wait_rc bf2 90 || fail "broker-failure: launching run never finished"
    rc=$(rc_of bf2)
    if [ "$rc" = 6 ] && grep -qE 'FAIL: RegisterLaunch (failed|refused)' "$U/bf2.err" && ! [ -s "$U/bf2.out" ]; then
        pass "broker-failure: launch aborted on RegisterLaunch failure (rc=6, no READY)"
    else
        fail "broker-failure: rc=$rc out=[$(cat "$U/bf2.out")] err=[$(tr '\n' ' ' < "$U/bf2.err")]"
    fi
    assert_chain_gone broker-failure
    systemctl unmask --runtime "$BROKER" >/dev/null 2>&1
    systemctl reset-failed "$BROKER" 2>/dev/null
    systemctl start "$BROKER"
    wait_for 30 broker_up && pass "broker-failure: broker restored" || fail "broker-failure: broker not restored"
    no_registration_after "$a" broker-failure
    finish ;;

caller-death)
    install_auth_rule
    agent_seam "password=$PW" || fail "caller-death: agent seam not applied"
    write_fake_secctx
    # (1) during the launch: the stand-in never reports.
    echo slow > "$W/fake-mode"; chmod 0644 "$W/fake-mode"
    bind_fake "$SECCTX" "$FAKES/secctx-standin" || fail "caller-death: cannot bind the stand-in"
    bridge_script cd1 --loopback "$(newloop)" --port "$(newport)" --token "$(token)"
    scope_run cd1 "$S/cd1.sh"
    if wait_for 60 sh -c "pgrep -fx 'sleep 601' >/dev/null"; then
        kill_caller cd1
        if wait_for 20 grep -q 'FAIL: caller went away during launch' "$U/cd1.err"; then
            pass "caller-death during launch: bridge noticed the caller's EOF"
        else
            fail "caller-death during launch: err=[$(tr '\n' ' ' < "$U/cd1.err")]"
        fi
    else
        fail "caller-death during launch: the chain never reached the stand-in: $(tr '\n' ' ' < "$U/cd1.err")"
    fi
    assert_chain_gone "caller-death during launch"
    unbind_all
    # (2) after READY: the real chain.
    bridge_script cd2 --loopback "$(newloop)" --port "$(newport)" --token "$(token)"
    scope_run cd2 "$S/cd2.sh"
    if wait_for 60 grep -q '^READY [0-9]*$' "$U/cd2.out"; then
        WP=$(sed -n 's/^READY //p' "$U/cd2.out")
        kill_caller cd2
        wait_for 20 sh -c "! kill -0 $WP 2>/dev/null" \
            && pass "caller-death after READY: waypipe client $WP reaped" \
            || fail "caller-death after READY: waypipe $WP still alive"
    else
        fail "caller-death after READY: no READY: $(tr '\n' ' ' < "$U/cd2.err")"
    fi
    assert_chain_gone "caller-death after READY"
    # (3) SIGKILL of the real admin wrapper (spawn-tier5) after the bridge is up.
    printf '#!/bin/bash\ncd /\nexec %q --loopback -p %q -- qdistro-test-window --title s65-cd3 >%q 2>%q\n' \
        "$SPAWN" "$(newport)" "$U/cd3.out" "$U/cd3.err" > "$S/cd3.sh"
    scope_run cd3 "$S/cd3.sh"
    if wait_for 90 grep -q 'bridge ready: waypipe client pid=' "$U/cd3.err"; then
        WP=$(sed -n 's/.*bridge ready: waypipe client pid=\([0-9]*\).*/\1/p' "$U/cd3.err" | head -1)
        SPW=$(ppid_of "$(ppid_of "$(ppid_of "$(ppid_of "$WP")")")")
        info "spawn-tier5 pid=$SPW ($(tr '\0' ' ' < "/proc/$SPW/cmdline" 2>/dev/null | head -c 120))"
        kill -KILL "$SPW" 2>/dev/null
        wait_for 20 sh -c "! kill -0 $WP 2>/dev/null" \
            && pass "caller-death (spawn-tier5 SIGKILL): bridge saw EOF and reaped waypipe $WP" \
            || fail "caller-death (spawn-tier5 SIGKILL): waypipe $WP still alive"
    else
        fail "caller-death (spawn-tier5 SIGKILL): bridge never ready: $(tail -c 800 "$U/cd3.err" | tr '\n' ';')"
    fi
    pkill -u admin -f 'qdistro-test-window --title s65-cd3' 2>/dev/null || true
    # The loopback waypipe SERVER stands in for the guest side (it runs in the
    # guest in --vm mode); it is spawn's own child, not part of the bridge
    # chain, so a SIGKILLed wrapper can orphan it. Report it, then end it.
    srv=$(pgrep -u admin -f "waypipe .*server -- qdistro-test-window --title s65-cd3" | tr '\n' ' ')
    [ -n "$srv" ] && info "orphaned loopback waypipe server (guest-side stand-in): $srv"
    pkill -u admin -f "waypipe .*server -- qdistro-test-window --title s65-cd3" 2>/dev/null || true
    assert_chain_gone "caller-death (spawn-tier5 SIGKILL)"
    finish ;;

postconditions)
    unbind_all
    if sha256sum -c --quiet "$W/sha" >/dev/null 2>&1 \
       && ! findmnt -rn -o TARGET | grep -qxE "$SECCTX|$WAYPIPE|$BRIDGE"; then
        pass "secctx-exec, waypipe, bridge, dbus-send, pkexec unchanged and not overmounted"
    else
        fail "fixed executables changed or overmounted: $(sha256sum -c "$W/sha" 2>&1 | grep -v ': OK' | tr '\n' ' ')"
    fi
    left=$(new_chain_procs)
    [ -z "$left" ] && pass "no tier-5 bridge chain process (root or admin) survives the file" \
                   || fail "surviving chain processes: $(tr '\n' ';' <<<"$left")"
    rootf=$(find /home/admin "$RT" /tmp /var/tmp -xdev \
              \( -path '/tmp/systemd-private-*' -o -path '/var/tmp/systemd-private-*' \) -prune -o \
              -newer "$W/mark" \( -user root -o -group root \) -print 2>/dev/null | head -20)
    [ -z "$rootf" ] && pass "no root-owned file created in admin-writable paths (/home/admin, $RT, /tmp, /var/tmp)" \
                    || fail "root-owned files created after setup: $(tr '\n' ' ' <<<"$rootf")"
    leg=""
    for d in /run/qdistro-tier5 /run/qdistro-tier5-boot /var/lib/qdistro/tier5-owned; do
        [ -e "$d" ] && leg="$leg $d"
    done
    [ -z "$leg" ] && pass "no root-run tier-5 lifecycle state (/run/qdistro-tier5*, tier5-owned)" \
                  || fail "legacy root tier-5 state present:$leg"
    sock=$(find "$RT" -maxdepth 1 -name 'wayland-secctx-*' -newer "$W/mark" 2>/dev/null)
    [ -z "$sock" ] && pass "no secctx listener socket from this file left in $RT" \
                   || fail "listener sockets left: $sock"
    finish ;;

teardown)
    unbind_all
    rm -rf "$FAKES"
    pkill -f '^sleep 6[0-9]{3}$' 2>/dev/null || true
    pkill -u admin -f 'qdistro-test-window --title s65-' 2>/dev/null || true
    rm -f "$AUTH_RULE" "$LOG_RULE" "$DROPIN"
    systemctl daemon-reload
    systemctl restart "$AGENT" 2>/dev/null || true
    systemctl stop "$LOGIN_UNIT" 2>/dev/null || true
    systemctl unmask --runtime "$BROKER" >/dev/null 2>&1 || true
    systemctl start "$BROKER" 2>/dev/null || true
    faillock --user admin --reset 2>/dev/null || true
    if [ -e "$SUDOERS_SAVED" ]; then mv -f "$SUDOERS_SAVED" "$SUDOERS"; fi
    rm -rf "$W"
    pass "s65 teardown done"
    finish ;;

*)
    echo "unknown case: $CASE" >&2; exit 2 ;;
esac
