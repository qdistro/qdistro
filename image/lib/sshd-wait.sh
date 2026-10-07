# shellcheck shell=bash
# image/lib/sshd-wait.sh — start sshd through the guest agent and wait for SSH
# auth, all inside ONE absolute deadline. Sourced by image/verify.sh; kept
# separate so tests/integration/vm/image-sshd-wait.bats can drive it with a
# fake clock, a fake agent and a fake SSH probe.
#
# The image never enables sshd (image/AGENTS.md), so the verifier starts it
# over the qemu-guest-agent channel. Run image-20261006T202319Z-14113 showed
# that a start can be refused (systemctl exit 4 = EXIT_NOPERMISSION: access
# denied or a refused/destructive transaction), so the wait loops retry it.
# Every retry step (unit-state check, start) is bounded by the time left
# before the caller's deadline and no new step starts after it. Only the
# diagnostics written after a refused start get their own small, separate
# bound (QDV_DIAG_TIMEOUT), so a wait ends no later than
# deadline + QDV_DIAG_TIMEOUT (+ one SSH probe's ConnectTimeout).
#
# Caller provides:
#   qga_root <shell> <timeout-s>  run as root in the guest; timeout <= 0 must
#                                 return non-zero without contacting the guest
#   remote <cmd...>               SSH into the guest as the test user
#   log / warn                    host-side messages
#   VERIFY_DIR                    run artifact directory
# Overridable for tests: qdv_now (epoch seconds), qdv_sleep <s>.

qdv_now()   { date +%s; }
qdv_sleep() { sleep "$1"; }

QDV_OP_TIMEOUT="${QDV_OP_TIMEOUT:-60}"      # cap for one agent step
QDV_DIAG_TIMEOUT="${QDV_DIAG_TIMEOUT:-15}"  # separate cap for diagnostics
QDV_POLL_S="${QDV_POLL_S:-5}"               # SSH probe interval
SSHD_START_FAILS=0
SSHD_START_LOGS=()

# qdv_budget <deadline> <cap> — seconds an operation may take: the time left
# before <deadline>, capped at <cap>; 0 once the deadline has passed.
qdv_budget() {
    local left=$(( $1 - $(qdv_now) ))
    [ "$left" -gt 0 ] || { echo 0; return; }
    [ "$left" -lt "$2" ] && echo "$left" || echo "$2"
}

# start_sshd <tag> <deadline> — systemctl start sshd.service through the
# agent; returns systemctl's exit code (98 without contacting the guest when
# the deadline has passed). A refusal records systemctl's message, the unit
# state and the journal in journal/sshd-start-<tag>.log.
start_sshd() {
    local tag="$1" deadline="$2" out rc budget logf
    budget=$(qdv_budget "$deadline" "$QDV_OP_TIMEOUT")
    [ "$budget" -gt 0 ] || return 98
    rc=0; out=$(qga_root 'systemctl start sshd.service' "$budget" 2>&1) || rc=$?
    [ "$rc" = 0 ] && return 0
    SSHD_START_FAILS=$((SSHD_START_FAILS + 1))
    warn "systemctl start sshd.service ($tag) returned exitcode=$rc: ${out:-<no output>}"
    mkdir -p "$VERIFY_DIR/journal"
    logf="$VERIFY_DIR/journal/sshd-start-$tag.log"
    SSHD_START_LOGS+=("$logf")
    {
        printf 'exitcode=%s\n%s\n--- diagnostics ---\n' "$rc" "$out"
        qga_root 'cat /proc/uptime; getenforce; systemctl is-system-running; systemctl list-jobs --no-pager; systemctl status --no-pager sshd.service sshd.socket; systemctl list-unit-files --no-pager "ssh*"; journalctl -b --no-pager -o short-monotonic -u sshd.service -u qemu-guest-agent.service | tail -n 80; journalctl -b --no-pager -o short-monotonic -g "avc:|destructive|denied|sshd" | tail -n 80' "$QDV_DIAG_TIMEOUT" 2>&1
    } > "$logf" 2>&1 || true
    log "sshd start diagnostics: $logf"
    return "$rc"
}

# ensure_sshd <tag> <deadline> — start sshd only if the unit is not active.
ensure_sshd() {
    local budget
    budget=$(qdv_budget "$2" "$QDV_OP_TIMEOUT")
    [ "$budget" -gt 0 ] || return 98
    qga_root 'systemctl is-active --quiet sshd.service' "$budget" >/dev/null 2>&1 && return 0
    start_sshd "$1" "$2"
}

# wait_for_ssh <phase> <deadline> <retry-every-n-probes> — start sshd, then
# probe SSH auth until it succeeds (0) or <deadline> passes (1). Every n-th
# failed probe re-checks the unit and retries a refused or lost start. No
# probe, check or start begins after <deadline>.
wait_for_ssh() {
    local phase="$1" deadline="$2" every="$3" n=0 nap
    start_sshd "$phase-1" "$deadline" \
        || warn "sshd not started yet ($phase; the SSH wait retries the start)"
    while [ "$(qdv_now)" -lt "$deadline" ]; do
        remote 'true' 2>/dev/null && return 0
        n=$((n + 1))
        if [ $((n % every)) = 0 ]; then
            ensure_sshd "$phase-retry-$n" "$deadline" || true
        fi
        nap=$(qdv_budget "$deadline" "$QDV_POLL_S")
        [ "$nap" -gt 0 ] || break
        qdv_sleep "$nap"
    done
    return 1
}

# qdv_ssh_failure_note — failure count and evidence paths for a fatal message.
qdv_ssh_failure_note() {
    if [ "$SSHD_START_FAILS" = 0 ]; then
        echo "no sshd start was refused"
    else
        echo "sshd start refused $SSHD_START_FAILS time(s); diagnostics: ${SSHD_START_LOGS[*]}"
    fi
}
