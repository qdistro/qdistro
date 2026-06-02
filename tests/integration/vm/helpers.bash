# Shared helpers for VM-gated bats tests. Source from test files with
#     load helpers
# (bats-core resolves helpers.bash relative to the .bats file).

# Absolute path to vm-exec. Prefer an override via VM_EXEC env,
# otherwise derive from the repo root.
: "${VM_NAME:?set VM_NAME to the qdwin VM name}"
if [[ -z "${VM_EXEC:-}" ]]; then
    _repo_root=$(git -C "$(dirname "${BATS_TEST_FILENAME}")" \
                     rev-parse --show-toplevel 2>/dev/null)
    VM_EXEC="${_repo_root}/scripts/vm/vm-exec"
fi

# SSH transport — used by enforcing-mode VMs where qemu-guest-agent is
# denied (virt_qemu_ga_t is too restricted under SELinux=enforcing).
# When VM_SSH_PORT is set, vm_run() routes through ssh on
# 127.0.0.1:$VM_SSH_PORT instead of qga. Optional knobs:
#   VM_SSH_USER       — default 'root'
#   VM_SSH_KEY        — default ~/.ssh/qdistro_enforcing_id_ed25519
#   VM_SSH_HOST       — default 127.0.0.1
#
# virt_qemu_ga_t is too
# restricted under enforcing".
: "${VM_SSH_USER:=root}"
: "${VM_SSH_KEY:=$HOME/.ssh/qdistro_enforcing_id_ed25519}"
: "${VM_SSH_HOST:=127.0.0.1}"

# Host IP as seen from inside the VM. Under SLIRP/qga this is the
# QEMU convention 10.0.2.2; under passt/SSH it's whatever the guest's
# default-route gateway happens to be (the host's outbound IP, since
# passt's shared-network mode places the guest on the host's LAN).
# Discovered lazily on first use and cached for the bats run.
_vm_host_ip_cache=""
vm_host_ip() {
    if [[ -n "$_vm_host_ip_cache" ]]; then
        printf '%s' "$_vm_host_ip_cache"
        return
    fi
    if [[ -n "${VM_SSH_PORT:-}" ]]; then
        _vm_host_ip_cache=$(ssh \
            -p "$VM_SSH_PORT" \
            -i "$VM_SSH_KEY" \
            -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR \
            -o ConnectTimeout=5 \
            -o BatchMode=yes \
            "$VM_SSH_USER@$VM_SSH_HOST" \
            "ip route | awk '/^default/ {print \$3; exit}'" 2>/dev/null)
    fi
    [[ -z "$_vm_host_ip_cache" ]] && _vm_host_ip_cache="10.0.2.2"
    printf '%s' "$_vm_host_ip_cache"
}

# vm_run <cmd> — exec a single-line command inside the VM and capture
# stdout+stderr into $output, exit status into $status. Routes via SSH
# if VM_SSH_PORT is set, otherwise via qemu-guest-agent.
#
# Under SSH transport (passt), the SLIRP-only 10.0.2.2 literal that
# many @test bodies hardcode for fetching staged drivers is rewritten
# to the discovered gateway IP. Under qga transport (SLIRP), 10.0.2.2
# stays as-is.
vm_run() {
    local cmd="$1"
    if [[ -n "${VM_SSH_PORT:-}" ]]; then
        local host_ip
        host_ip="$(vm_host_ip)"
        cmd="${cmd//10.0.2.2/$host_ip}"
        run ssh \
            -p "$VM_SSH_PORT" \
            -i "$VM_SSH_KEY" \
            -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR \
            -o ConnectTimeout=5 \
            -o BatchMode=yes \
            "$VM_SSH_USER@$VM_SSH_HOST" \
            "$cmd"
    else
        run "$VM_EXEC" "$VM_NAME" "$cmd"
    fi
}

# vm_run_admin <cmd> — run a command inside the VM as the admin user
# (uid 1000) with a real PAM session and admin's --user systemd.
# Routes through the same transport as vm_run (qga or ssh) but wraps
# the command in `runuser -l admin -c '...'`. Use this for any test
# step that needs admin's user manager (systemctl --user, qdlocker.sock,
# qdshell.sock, noctalia-shell.service, etc).
vm_run_admin() {
    local cmd="$1"
    # Escape single quotes for runuser's outer 'cmd' string.
    local escaped="${cmd//\'/\'\\\'\'}"
    vm_run "runuser -l admin -c '$escaped'"
}

# start_user_session — idempotent: ensure admin's user manager is
# running and noctalia-shell + qdlocker are active. Tests that need
# the qdshell GUI alive call this in their setup_file().
# Returns 0 on success; non-zero if /run/user/1000/wayland-1 didn't
# appear within 30s (caller should fail_loud).
_user_session_started=""
start_user_session() {
    [[ -n "$_user_session_started" ]] && return 0
    vm_run "loginctl enable-linger admin >/dev/null 2>&1 || true"
    vm_run_admin "systemctl --user start noctalia-shell.service" || true
    # Wait up to 30s for the wayland socket (see wait_for_socket below).
    if wait_for_socket /run/user/1000/wayland-1 30; then
        _user_session_started=1
    fi
    [[ -n "$_user_session_started" ]]
}

# assert_success — bats-assert-like tiny shim (don't want the dep).
assert_success() {
    if [[ "$status" -ne 0 ]]; then
        echo "--- command failed (exit=$status) ---" >&2
        echo "$output" >&2
        return 1
    fi
}

# assert_output_contains <substr> — grep-style check.
assert_output_contains() {
    local needle=$1
    if ! grep -qF -- "$needle" <<<"$output"; then
        echo "--- expected substring '$needle' in output ---" >&2
        echo "$output" >&2
        return 1
    fi
}

# require <description> — fail the test loudly when a precondition
# check (vm_run, command -v, [ -x ...]) returned non-zero. Use this
# in place of the previous `skip "<dep missing>"` pattern: missing
# deps should be loud failures, not silent skips, so bake / install
# regressions surface immediately instead of masquerading as "all
# tests pass (most skipped)" green CI.
#
# Pattern:
#   vm_run "command -v xfreerdp >/dev/null"
#   require "xfreerdp not installed on VM (need freerdp3 package)"
require() {
    if [[ "$status" -ne 0 ]]; then
        echo "--- MISSING REQUIREMENT: $1 ---" >&2
        echo "vm exit=$status, output below:" >&2
        echo "$output" >&2
        return 1
    fi
}

# stage_http_8765 <stage_dir> — idempotently (re)start the host-side
# python3 http.server on port 8765 rooted at <stage_dir>, so VM tests
# can fetch driver scripts via http://10.0.2.2:8765/<name>.
#
# Round-6 root cause: spin-test-vm leaves an http.server bound to 8765
# whose cwd is a tempdir that gets deleted on spin exit. The kernel
# then reports the process cwd as "(deleted)" and python serves an
# HTML 404 page for every request, which makes the VM-side
# `curl … | bash` choke on `<!DOCTYPE HTML>`. Detecting a stale server
# (port-bound but serving wrong root) by content-sniff is fragile, so
# we always kill anything on 8765 and spawn fresh — costs ~200ms but
# is deterministic. PID is written to /tmp/qdistro-bats-http.pid for
# teardown / debugging.
stage_http_8765() {
    local stage_dir="$1"
    [[ -d "$stage_dir" ]] || { echo "stage_http_8765: not a dir: $stage_dir" >&2; return 1; }
    pkill -f "python3 -m http.server 8765" 2>/dev/null || true
    local i
    for ((i=0; i<20; i++)); do
        ss -tln 2>/dev/null | grep -q ":8765 " || break
        sleep 0.1
    done
    (
        cd "$stage_dir" || exit 1
        nohup python3 -m http.server 8765 \
            >/tmp/qdistro-bats-http.log 2>&1 </dev/null 3>&- 4>&- 5>&- &
        echo $! >/tmp/qdistro-bats-http.pid
        disown "$!" 2>/dev/null || true
    )
    for ((i=0; i<30; i++)); do
        curl -sf -o /dev/null "http://127.0.0.1:8765/" && return 0
        sleep 0.1
    done
    echo "stage_http_8765: server on 8765 did not become reachable" >&2
    return 1
}

# fail_loud <description> — alias for `require` when the test wants
# to fail unconditionally on a control-flow branch (e.g. after the
# helper script emits "SKIP:" in its output). Same shape as the
# pre-2026-05-14 `skip "..."` calls; deps should be in the bake or
# the bake is broken — silent skips masked too many missing-dep
# regressions.
fail_loud() {
    echo "--- TEST FAILED: $* ---" >&2
    return 1
}

# ---------------------------------------------------------------------------
# VM-driver helper layer
#
# A thin, NixOS-Machine-API-inspired layer built ON TOP of vm_run /
# vm_run_admin (which already route via qga or ssh). These helpers give
# tests and qci evidence a uniform, greppable structure and replace the
# hand-rolled `for ((i=0; i<N; i++)); do ... sleep 1; done` polling loops
# scattered across individual specs.
#
# Conventions shared by every wait_* helper below:
#   - default timeout is 30 seconds; pass an integer to override;
#   - polling cadence is ~once per second;
#   - on success they return 0 quietly;
#   - on timeout they emit a loud `--- TIMEOUT: ...` diagnostic to stderr
#     (matching the require/fail_loud loud-failure style) and return 1.
# All guest interaction goes through vm_run / vm_run_admin — never shell
# out to vm-exec/ssh directly from here.
# ---------------------------------------------------------------------------

# step <description> — print a grouped, greppable marker line to stderr,
# e.g. `--- step: start qdshell ---`. Use at the top of a logical block of
# a test so the surrounding vm_run output and qci evidence are easy to scan
# and bisect. No global state; just structured echo.
step() {
    echo "--- step: $* ---" >&2
}

# subtest <description> — like step, but for a coarser grouping of related
# steps within a single @test, e.g. `--- subtest: locker unlock flow ---`.
subtest() {
    echo "--- subtest: $* ---" >&2
}

# wait_for_unit <unit> [timeout_s=30] [--user] — poll
# `systemctl is-active <unit>` until it reports `active` or the timeout
# elapses. With --user the unit is queried in admin's --user manager via
# vm_run_admin; otherwise the system manager via vm_run. On timeout, dump
# the unit's recent `systemctl status` and journal tail to stderr and
# return 1.
wait_for_unit() {
    local unit="" timeout=30 user=0 arg
    for arg in "$@"; do
        case "$arg" in
            --user) user=1 ;;
            *[!0-9]*|'') [[ -z "$unit" ]] && unit="$arg" ;;
            *) timeout="$arg" ;;
        esac
    done
    [[ -n "$unit" ]] || { echo "--- wait_for_unit: missing unit name ---" >&2; return 2; }

    local userflag="" runner=vm_run
    if [[ "$user" -eq 1 ]]; then
        userflag="--user "
        runner=vm_run_admin
    fi

    local i
    for ((i=0; i<timeout; i++)); do
        "$runner" "systemctl ${userflag}is-active --quiet '$unit'"
        [[ "$status" -eq 0 ]] && return 0
        sleep 1
    done

    echo "--- TIMEOUT: unit '$unit' not active after ${timeout}s${userflag:+ (--user)} ---" >&2
    "$runner" "systemctl ${userflag}status --no-pager --lines=20 '$unit' 2>&1; echo '--- journal ---'; journalctl ${userflag}-u '$unit' --no-pager --lines=30 2>&1"
    echo "$output" >&2
    return 1
}

# wait_for_bus_name <name> [timeout_s=30] [--user] — poll the session/system
# bus until <name> is an owned (not merely activatable) well-known name, or
# the timeout elapses. With --user the name is queried on admin's session bus
# via vm_run_admin; otherwise the system bus via vm_run.
#
# Type=dbus units report "active" only once their BusName is acquired, but a
# freshly-provisioned graphical session can bounce a per-user daemon once
# during bring-up (clean Stop/Start), so a single-shot `busctl list` right
# after `systemctl start` can race the settle window — exactly the flake that
# dropped org.qdistro.Compositor (the last-started 9e daemon) from the check.
# Poll instead of asserting once.
wait_for_bus_name() {
    local name="" timeout=30 user=0 arg
    for arg in "$@"; do
        case "$arg" in
            --user) user=1 ;;
            *[!0-9]*|'') [[ -z "$name" ]] && name="$arg" ;;
            *) timeout="$arg" ;;
        esac
    done
    [[ -n "$name" ]] || { echo "--- wait_for_bus_name: missing name ---" >&2; return 2; }
    [[ "$name" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "--- wait_for_bus_name: invalid bus name '$name' ---" >&2; return 2; }

    # The session 9e daemons own their names on admin's (uid 1000) bus, which
    # is only reachable as that user: under qga `vm_run` executes as root with
    # no XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS, so a root `busctl --user`
    # binds root's empty user bus and never sees them. Route --user through
    # vm_run_admin (runuser -l admin), matching wait_for_unit and the suite's
    # other `systemctl --user`/`busctl --user` callers.
    local userflag="" runner=vm_run
    if [[ "$user" -eq 1 ]]; then
        userflag="--user "
        runner=vm_run_admin
    fi

    local i
    for ((i=0; i<timeout; i++)); do
        # An owned name appears in `busctl list` with a numeric PID; an
        # activatable-but-unstarted name shows "(activatable)". Match the
        # first column literally so regex metacharacters in a name cannot
        # false-match a different bus name.
        "$runner" "busctl ${userflag}list --no-legend 2>/dev/null | awk -v name='${name}' '\$1 == name && \$2 != \"(activatable)\" { found=1 } END { exit found ? 0 : 1 }'"
        [[ "$status" -eq 0 ]] && return 0
        sleep 1
    done
    echo "--- TIMEOUT: bus name '$name' not owned after ${timeout}s${userflag:+ (--user)} ---" >&2
    return 1
}

# wait_for_socket <path> [timeout_s=30] — poll `test -S <path>` inside the
# VM (via vm_run) until the unix socket exists or the timeout elapses.
wait_for_socket() {
    local path="$1" timeout="${2:-30}"
    [[ -n "$path" ]] || { echo "--- wait_for_socket: missing path ---" >&2; return 2; }
    [[ "$timeout" =~ ^[0-9]+$ ]] || { echo "--- wait_for_socket: timeout must be an integer (got '$timeout'); signature is: wait_for_socket <path> [timeout_s=30] ---" >&2; return 2; }
    local i
    for ((i=0; i<timeout; i++)); do
        vm_run "test -S '$path'"
        [[ "$status" -eq 0 ]] && return 0
        sleep 1
    done
    echo "--- TIMEOUT: socket '$path' did not appear after ${timeout}s ---" >&2
    return 1
}

# wait_for_file <path> [timeout_s=30] — poll `test -e <path>` inside the VM
# (via vm_run) until the path exists or the timeout elapses.
wait_for_file() {
    local path="$1" timeout="${2:-30}"
    [[ -n "$path" ]] || { echo "--- wait_for_file: missing path ---" >&2; return 2; }
    [[ "$timeout" =~ ^[0-9]+$ ]] || { echo "--- wait_for_file: timeout must be an integer (got '$timeout'); signature is: wait_for_file <path> [timeout_s=30] ---" >&2; return 2; }
    local i
    for ((i=0; i<timeout; i++)); do
        vm_run "test -e '$path'"
        [[ "$status" -eq 0 ]] && return 0
        sleep 1
    done
    echo "--- TIMEOUT: file '$path' did not appear after ${timeout}s ---" >&2
    return 1
}

# wait_for_journal_line <pattern> [timeout_s=30] [--user] — poll
# journalctl for a line matching <pattern> that was logged since this
# helper started (anchored with --since), until a match is found or the
# timeout elapses. System journal by default (via vm_run); with --user the
# admin per-user journal is queried via vm_run_admin. <pattern> is passed
# to `grep -E` so callers may use extended regex; literal strings work too.
wait_for_journal_line() {
    local pattern="" timeout=30 user=0 arg
    for arg in "$@"; do
        case "$arg" in
            --user) user=1 ;;
            *[!0-9]*|'') [[ -z "$pattern" ]] && pattern="$arg" ;;
            *) timeout="$arg" ;;
        esac
    done
    [[ -n "$pattern" ]] || { echo "--- wait_for_journal_line: missing pattern ---" >&2; return 2; }

    local userflag="" runner=vm_run
    if [[ "$user" -eq 1 ]]; then
        userflag="--user "
        runner=vm_run_admin
    fi

    # Anchor the search at "now" so we only see lines logged from this
    # point forward, not stale matches from earlier in the boot.
    local since
    "$runner" "date '+%Y-%m-%d %H:%M:%S'"
    since="$output"
    [[ -n "$since" ]] || since="-1min"

    # Escape single quotes in the pattern for the inner shell command.
    local esc_pattern="${pattern//\'/\'\\\'\'}"
    local esc_since="${since//\'/\'\\\'\'}"

    local i
    for ((i=0; i<timeout; i++)); do
        "$runner" "journalctl ${userflag}--no-pager --since='$esc_since' 2>/dev/null | grep -E -- '$esc_pattern'"
        [[ "$status" -eq 0 ]] && return 0
        sleep 1
    done
    echo "--- TIMEOUT: no journal line matching '$pattern' after ${timeout}s${userflag:+ (--user)} ---" >&2
    return 1
}

# wait_until_succeeds <cmd> [timeout_s=30] — poll an arbitrary single-line
# command via vm_run until it exits 0 or the timeout elapses. The most
# general primitive; the more specific wait_for_* helpers are preferred
# where they fit because their timeout diagnostics are richer.
wait_until_succeeds() {
    local cmd="$1" timeout="${2:-30}"
    [[ -n "$cmd" ]] || { echo "--- wait_until_succeeds: missing command ---" >&2; return 2; }
    [[ "$timeout" =~ ^[0-9]+$ ]] || { echo "--- wait_until_succeeds: timeout must be an integer (got '$timeout'); signature is: wait_until_succeeds <cmd> [timeout_s=30] ---" >&2; return 2; }
    local i
    for ((i=0; i<timeout; i++)); do
        vm_run "$cmd"
        [[ "$status" -eq 0 ]] && return 0
        sleep 1
    done
    echo "--- TIMEOUT: command did not succeed within ${timeout}s: $cmd ---" >&2
    echo "$output" >&2
    return 1
}

# Worked example (illustrative; not executed):
#
#   @test "qdshell comes up for admin" {
#       step "enable linger and start the user session"
#       start_user_session || fail_loud "user session did not start"
#
#       subtest "qdshell services and sockets"
#       step "wait for noctalia-shell --user unit"
#       wait_for_unit noctalia-shell.service 30 --user \
#           || fail_loud "noctalia-shell.service never went active"
#
#       step "wait for the qdshell control socket"
#       wait_for_socket /run/user/1000/qdshell.sock 15 \
#           || fail_loud "qdshell.sock never appeared"
#   }

# ---------------------------------------------------------------------------
# Per-assertion evidence layer ("CheckResult")
#
# Modeled on LevitateOS's CheckResult discipline and the qdistro evidence
# rules in ci/prompts/anti-cheat-guidance.md and tests/AGENTS.md:
#
#   Pass { evidence }        — a PASS is not a result unless it CITES the
#                              actual value/output/path that proves it.
#   Fail { expected, actual }— a FAIL must show BOTH the expected string and
#                              what was actually observed; "did not match" is
#                              not enough.
#   ensures: <capability>    — what user-visible capability the assertion
#                              protects. Stated right before a check so a
#                              failure explains its impact, not just its diff.
#
# These are pure-bash, no new deps, no network. Output goes to stderr (so it
# interleaves with step/subtest/require diagnostics and is captured in the
# qci per-test log) in a STABLE, machine-greppable shape so report.py — or a
# human running `grep` over a captured log — can extract evidence later:
#
#   --- ensures: <capability-description> ---
#   --- CHECK pass: <message> | evidence: <...> ---
#   --- CHECK fail: <message> | expected: <...> | actual: <...> ---
#
# Skip is deliberately NOT provided here: per tests/AGENTS.md and the
# anti-cheat guidance, a missing precondition in a VM bats test is a loud
# require/fail_loud, never a silent skip.
# ---------------------------------------------------------------------------

# check_pass <message> [evidence] — record a passing assertion that CITES its
# evidence. Prints a greppable
#     --- CHECK pass: <message> | evidence: <...> ---
# line to stderr (evidence omitted from the suffix when not supplied, but
# supplying the actual proving value — a path, a size, an output line — is the
# whole point: a bare PASS is not a result). Always returns 0.
check_pass() {
    local message="$1" evidence="${2:-}"
    if [[ -n "$evidence" ]]; then
        echo "--- CHECK pass: $message | evidence: $evidence ---" >&2
    else
        echo "--- CHECK pass: $message ---" >&2
    fi
    return 0
}

# check_fail <expected> <actual> [message] — record a failing assertion that
# shows BOTH sides of the comparison. Prints a greppable
#     --- CHECK fail: <message> | expected: <expected> | actual: <actual> ---
# line to stderr and returns 1, so a caller can `check_fail ... || return 1`
# or rely on the non-zero status to fail the @test. The message is optional;
# when omitted the line still carries expected/actual.
check_fail() {
    local expected="$1" actual="$2" message="${3:-}"
    if [[ -n "$message" ]]; then
        echo "--- CHECK fail: $message | expected: $expected | actual: $actual ---" >&2
    else
        echo "--- CHECK fail: expected: $expected | actual: $actual ---" >&2
    fi
    return 1
}

# ensures <capability-description> — declare the user-visible capability the
# next assertion protects. Prints a greppable
#     --- ensures: <capability-description> ---
# line to stderr. Call it immediately before a check so that a failure in the
# captured log is preceded by WHY the check exists ("a denied cross-silo
# clipboard transfer stays denied"), not just a bare expected/actual diff.
# No global state; just structured echo. Always returns 0.
ensures() {
    echo "--- ensures: $* ---" >&2
    return 0
}

# assert_eq_evidence <expected> <actual> <ensures-msg> — one-line convenience
# that combines ensures + an equality compare + check_pass/check_fail, so a
# test author gets evidence on BOTH the passing and failing path from a single
# call. On equality it emits the ensures line and a CHECK pass citing the
# (matched) value as evidence, returning 0. On mismatch it emits the ensures
# line and a CHECK fail showing expected vs actual, returning 1.
#
#   assert_eq_evidence "active" "$output" \
#       "the qdlocker unit stays running so the screen can be locked"
assert_eq_evidence() {
    local expected="$1" actual="$2" ensures_msg="$3"
    ensures "$ensures_msg"
    if [[ "$expected" == "$actual" ]]; then
        check_pass "$ensures_msg" "$actual"
        return 0
    fi
    check_fail "$expected" "$actual" "$ensures_msg"
    return 1
}

# Worked example (illustrative; not executed):
#
#   @test "denied cross-silo clipboard transfer stays denied" {
#       step "attempt a clipboard copy from work silo into personal silo"
#       vm_run_admin "qdclip --from work --to personal --paste 2>&1; echo rc=\$?"
#
#       # One line: declares what it protects, compares, and cites evidence
#       # on both the passing and failing path.
#       assert_eq_evidence "rc=1" "$(grep -o 'rc=[0-9]*' <<<"$output")" \
#           "a denied cross-silo clipboard transfer stays denied" \
#           || fail_loud "cross-silo clipboard transfer was NOT denied"
#
#       # Or, when the comparison is richer than equality, drive the
#       # primitives directly so the PASS still cites real evidence:
#       subtest "verify the broker logged the denial"
#       ensures "the broker records every cross-silo denial for audit"
#       if grep -q 'DENY clipboard work->personal' <<<"$output"; then
#           check_pass "broker logged the clipboard denial" \
#               "$(grep -m1 'DENY clipboard' <<<"$output")"
#       else
#           check_fail "a 'DENY clipboard work->personal' audit line" \
#               "${output:-<no broker output>}" \
#               "broker did not log the cross-silo clipboard denial"
#           fail_loud "missing broker audit line for clipboard denial"
#       fi
#   }
