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
        # CAPTURE THROUGH A FILE, NOT THROUGH bats' `run` PIPE.
        # bats-core's `run` merges streams with `bats_merge_stdout_and_stderr`,
        # i.e. `"$@" 2>&1`, INSIDE a command substitution
        # (lib/bats-core/test_functions.bash, bats 1.14). That hands vm-exec's
        # fd 2 to the substitution's pipe, and every virsh/jq descendant
        # vm-exec starts inherits it. vm-exec redirects its own children's
        # fd 1 to an internal capture file, but not fd 2 -- so a descendant
        # that outlives vm-exec holds the pipe open and the shell waits for the
        # PIPE, not for vm-exec. The test then hangs after the guest command is
        # long dead, with no timeout able to help.
        #
        # So vm-exec is run FIRST, with both descriptors on a private capture
        # file that is opened and unlinked before it starts (the shape of
        # bounded_run() in scripts/vm/vm-exec), and `run` is then pointed at a
        # bounded replay of that file. `$status`, `$output` and `$lines` come
        # out as before -- merged stdout+stderr, vm-exec's exit status -- but
        # the only thing `run`'s pipe ever holds is our own reader, which has
        # no descendant to outlive it.
        #
        # The reader is `head -c`, not `cat`. A plain `cat` is not an
        # independently bounded reader: it runs until EOF of a file a surviving
        # descendant may still be appending to, so its finite completion was
        # borrowed from the writer's behaviour rather than guaranteed here
        # (sol, todo/reviews/qci-A-260917-sol-review.md §6). The cap is
        # deliberately generous -- these are test diagnostics -- but it is a
        # cap.
        #
        # This closes the hazard for every caller that goes through vm_run. It
        # does NOT close it for a test that calls `run "$VM_EXEC" ...`
        # directly; those sites are listed in the round-9 caller-audit report.
        # Nor does it say anything about callers outside this file: the GUI
        # agent's own run-time driver scripts are the dominant vm-exec callers
        # and carry the same defect. They cannot be linted (they are written
        # during the run), so they are addressed in the agent prompt instead.
        local _vr_cf _vr_w _vr_r _vr_rc=0
        # SETUP FAILURE IS AN INFRASTRUCTURE FAULT, reported as 125 with a
        # diagnostic -- the same convention as replay, stat and flag-file
        # failure. `run false` gave status=1 with empty output and empty
        # stderr, which is indistinguishable from a guest command that RAN and
        # returned 1; here the command never ran at all (astra, A8 finding 1,
        # the one row in its ten-site fault matrix where vm_run disagreed with
        # every other site). `run bash -c 'exit N'` is used rather than
        # assigning $status directly so Bats' $output/$lines stay coherent.
        _vr_setup_fail() {   # <why>
            echo "vm_run: capture setup FAILED for '$cmd' ($1); the command was NOT run" >&2
            run bash -c 'exit 125'
        }
        if ! _vr_cf=$(mktemp "${BATS_TEST_TMPDIR:-${TMPDIR:-/tmp}}/vm-run.XXXXXXXX"); then
            _vr_setup_fail "could not create the capture file"; return
        fi
        # Checked, including the unlink: unchecked, a failing open or rm left
        # the capture NAMED while the command ran and reported the guest's
        # status as if nothing were wrong (sol A2 section 1).
        if ! exec {_vr_w}>"$_vr_cf"; then
            rm -f "$_vr_cf"; _vr_setup_fail "could not open the capture for writing"; return
        fi
        if ! exec {_vr_r}<"$_vr_cf"; then
            exec {_vr_w}>&-; rm -f "$_vr_cf"
            _vr_setup_fail "could not open the capture for reading"; return
        fi
        if ! rm -f "$_vr_cf" || [ -e "$_vr_cf" ]; then
            exec {_vr_w}>&- {_vr_r}<&-
            _vr_setup_fail "the capture could not be unlinked and would have stayed NAMED"
            return
        fi
        "$VM_EXEC" "$VM_NAME" "$cmd" \
            >&"$_vr_w" 2>&"$_vr_w" {_vr_w}>&- {_vr_r}<&- || _vr_rc=$?
        exec {_vr_w}>&-
        local _vr_cap=${QCI_VM_RUN_CAP_BYTES:-4194304}
        case "$_vr_cap" in
            ''|*[!0-9]*|0|0*)
                echo "vm_run: QCI_VM_RUN_CAP_BYTES must be a positive integer number of bytes, got '$_vr_cap'" >&2
                exec {_vr_w}>&- {_vr_r}<&- 2>/dev/null || :
                run bash -c 'exit 125'
                return ;;
        esac
        # The replay's OWN status must not be overwritten by the producer's.
        # `head -c ...; exit "$1"' discarded a failed replay and reported the
        # guest's success with empty output (sol A4 finding 1). 125 marks the
        # capture failure; the guest status is only reported once the bytes
        # were actually delivered.
        #
        # The replay runs in a subprocess because Bats' `run` must capture it,
        # so a bare 125 is AMBIGUOUS -- a guest command can exit 125 too. A
        # non-empty flag file disambiguates, which is what lets the diagnostic
        # be truthful rather than a guess. vm_run got the status right but said
        # nothing about why (astra, A-astra finding 3).
        local _vr_flag _vr_size
        # A failed mktemp here is an INFRASTRUCTURE fault, and the command has
        # already run: `run false` reported status=1 with empty output and no
        # explanation, which reads exactly like an ordinary guest exit 1 and
        # throws away a capture that exists (astra, A6 finding 4).
        if ! _vr_flag=$(mktemp "${BATS_TEST_TMPDIR:-${TMPDIR:-/tmp}}/vm-run-flag.XXXXXXXX"); then
            exec {_vr_r}<&-
            echo "vm_run: could not create the replay flag file for '$cmd'; the capture is UNREADABLE, not empty (the guest itself exited $_vr_rc)" >&2
            run bash -c 'exit 125'
            return
        fi
        run bash -c 'head -c "$2" <&3 || { printf R > "$3"; exit 125; }; exit "$1"' \
            _ "$_vr_rc" "$_vr_cap" "$_vr_flag" 3<&"$_vr_r"
        if [ -s "$_vr_flag" ]; then
            echo "vm_run: capture replay FAILED for '$cmd'; the output is UNAVAILABLE, not empty (the guest itself exited $_vr_rc)" >&2
            status=125
        fi
        rm -f "$_vr_flag"
        # COMPLETENESS. vm_run had no check at all: with a 4-byte cap and a
        # successful 4102-byte producer it reported status=0 out=[XXXX],
        # losing everything past the cap silently (astra, A-astra finding 2).
        #
        # WHAT THIS DOES NOT ESTABLISH: that the guest's output was complete.
        # The previous version compared the size against the PARENT's
        # `ulimit -f -H` and claimed that. It cannot -- `-H` is the hard limit
        # while writes obey the SOFT one, and the writer is a child (or a
        # descendant of one) whose limit this process does not know. `>=`
        # against it also failed a HEALTHY exact-fit write with a fatal 125
        # asserting the output "was cut off" (astra, A6 findings 1 and 2).
        # Detecting a writer's own EFBIG truncation needs a completion marker
        # from the writer, which does not exist yet. This reports the one
        # thing it can see: bytes stored past what the replay returned.
        if ! _vr_size=$(stat -Lc %s "/proc/self/fd/$_vr_r" 2>/dev/null); then
            echo "vm_run: could not stat the capture for '$cmd'; whether the output is complete is UNKNOWN, not verified" >&2
            status=125
        elif [ "$_vr_size" -gt "$_vr_cap" ]; then
            echo "vm_run: output for '$cmd' (${_vr_size} bytes) exceeded the ${_vr_cap}-byte replay cap; \$output is a PREFIX and any marker beyond the cap is lost. Raise QCI_VM_RUN_CAP_BYTES." >&2
            status=125
        fi
        exec {_vr_r}<&-
    fi
}

# vm_run_admin <cmd> — run a command inside the VM as the admin user
# (uid 1000) with a real PAM session and admin's --user systemd.
# Routes through the same transport as vm_run (qga or ssh) but wraps
# the command in `runuser -l admin -c '...'`. Use this for any test
# step that needs admin's user manager (systemctl --user, qdlocker.sock,
# qdshell.sock, qdshell.service, etc).
vm_run_admin() {
    local cmd="$1"
    # Escape single quotes for runuser's outer 'cmd' string.
    local escaped="${cmd//\'/\'\\\'\'}"
    vm_run "runuser -l admin -c '$escaped'"
}

# start_user_session — idempotent: ensure admin's user manager is
# running and the qdwin compositor + qdshell are active. Tests that need
# the qdshell GUI alive call this in their setup_file().
# Returns 0 on success; non-zero if /run/user/1000/wayland-1 didn't
# appear within 30s (caller should fail_loud).
_user_session_started=""
start_user_session() {
    [[ -n "$_user_session_started" ]] && return 0
    vm_run "loginctl enable-linger admin >/dev/null 2>&1 || true"
    vm_run_admin "systemctl --user start qdwin-session.target" || true
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

# ---------------------------------------------------------------------------
# Driver staging over a PRIVATE, free-port HTTP server (parallel-safe).
#
# stage_vm_driver <script_name> stages tests/integration/vm/<script_name> and
# serves it to the VM over a python http.server bound to a FREE port. This
# replaces the old fixed host-global ports (:8765 / :8768) that collided between
# concurrent CI workers — a worker could silently reuse a foreign/stale server
# squatting the port (one even pkill'd sibling workers' servers).
#
# It sets QDISTRO_BATS_HTTP_PORT for the VM-side fetch and CONTENT-VALIDATES
# (curl … | cmp) that the chosen port actually serves OUR file before returning,
# so a squatter can't masquerade as the stager (it picks a new free port). The
# server roots at a private per-FILE dir under BATS_FILE_TMPDIR and is reaped by
# reap_vm_drivers, which each test wires into teardown_file.
#
# VM-side fetch — use DOUBLE quotes so the port expands in the test shell:
#   vm_run "curl -fsS -o /tmp/x.sh \
#       http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/<script_name> && bash /tmp/x.sh"
# ---------------------------------------------------------------------------

# Private per-file serving dir. BATS_FILE_TMPDIR persists across a file's @tests
# and is removed by bats after teardown_file; the per-pid fallback keeps the
# helper usable outside a bats run.
_qd_driver_stage_dir() {
    printf '%s/qd-driver-stage' "${BATS_FILE_TMPDIR:-${TMPDIR:-/tmp}/qd-drivers-$(id -u)-$$}"
}

_qd_pick_free_port() {
    python3 - <<'PY'
import socket
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    print(s.getsockname()[1])
PY
}

stage_vm_driver() {
    local script_name="$1"
    local src stage_dir staged portfile pidfile base i
    src="$(dirname "$BATS_TEST_FILENAME")/$script_name"
    [ -f "$src" ] || fail_loud "driver script not found at $src"

    stage_dir="$(_qd_driver_stage_dir)"
    mkdir -p "$stage_dir"
    base="$(basename "$script_name")"
    staged="$stage_dir/$base"
    cp "$src" "$staged"
    portfile="$stage_dir/.port"
    pidfile="$stage_dir/.pids"

    # Reuse a server already started for this file if it still serves our dir.
    if [ -f "$portfile" ]; then
        QDISTRO_BATS_HTTP_PORT="$(cat "$portfile")"
        if curl -fsS "http://127.0.0.1:${QDISTRO_BATS_HTTP_PORT}/$base" 2>/dev/null | cmp -s - "$staged"; then
            export QDISTRO_BATS_HTTP_PORT
            return 0
        fi
    fi

    # Start a fresh server on a free port (root it at our private dir). The
    # free-port pick is a TOCTOU: another worker can grab the port between our
    # bind(0) probe and http.server's bind, in which case our python exits and
    # validation fails. Retry a NEW port a few times rather than failing the
    # whole test on a transient race.
    local attempt pid
    for attempt in 1 2 3 4 5; do
        QDISTRO_BATS_HTTP_PORT="$(_qd_pick_free_port)" || fail_loud "could not choose a free HTTP port"
        pid=""
        (
            cd "$stage_dir" || exit 1
            nohup python3 -m http.server "$QDISTRO_BATS_HTTP_PORT" \
                >"$stage_dir/.http-${QDISTRO_BATS_HTTP_PORT}.log" 2>&1 </dev/null 3>&- 4>&- 5>&- &
            echo $! >>"$pidfile"
            disown "$!" 2>/dev/null || true
        )
        pid="$(tail -n1 "$pidfile" 2>/dev/null)"
        echo "$QDISTRO_BATS_HTTP_PORT" >"$portfile"
        export QDISTRO_BATS_HTTP_PORT

        # Wait until the server serves OUR staged file (content-validated, not
        # just port-bound) — proves it's our process and not a squatter.
        for ((i=0; i<50; i++)); do
            curl -fsS "http://127.0.0.1:${QDISTRO_BATS_HTTP_PORT}/$base" 2>/dev/null \
                | cmp -s - "$staged" && return 0
            sleep 0.1
        done
        # This port lost the race (or the server died). Reap it and try another.
        [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    done
    fail_loud "driver stager could not serve $base after 5 free-port attempts"
}

# reap_vm_drivers — kill any http.server stage_vm_driver started for this file.
# Wire into teardown_file. A no-op when nothing was staged.
reap_vm_drivers() {
    local stage_dir pidfile pid
    stage_dir="$(_qd_driver_stage_dir)"
    pidfile="$stage_dir/.pids"
    [ -f "$pidfile" ] || return 0
    while read -r pid; do
        [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    done < "$pidfile"
    rm -f "$pidfile"
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
#       step "wait for qdshell --user unit"
#       wait_for_unit qdshell.service 30 --user \
#           || fail_loud "qdshell.service never went active"
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
