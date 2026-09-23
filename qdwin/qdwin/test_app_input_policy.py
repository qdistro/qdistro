#!/usr/bin/env python3
"""Pin real key-transition mechanics for application GUI scenarios."""

from pathlib import Path
import re
import sys


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    if len(sys.argv) != 5:
        return fail("usage: test_app_input_policy.py helper.sh audacity.md rdp.md xterm.md")
    helper = Path(sys.argv[1]).read_text(encoding="utf-8")
    audacity = Path(sys.argv[2]).read_text(encoding="utf-8")
    rdp = Path(sys.argv[3]).read_text(encoding="utf-8")
    xterm = Path(sys.argv[4]).read_text(encoding="utf-8")

    required = (
        "qdwin_apps_qmp_key()",
        '\\"execute\\": \\"input-send-event\\"',
        "qdwin_apps_chord()",
        'qdwin_apps_qmp_key "$key" down',
        'qdwin_apps_qmp_key "$key" up',
    )
    missing = [token for token in required if token not in helper]
    if missing:
        return fail(f"app input helper missing real-transition token(s): {missing}")
    if '$QDWIN_VIRSH send-key' in helper:
        return fail("app helper regressed to simultaneous virsh send-key injection")
    if "qdwin_apps_chord alt -- f" not in audacity:
        return fail("Audacity File-menu assertion does not use a modifier-held chord")

    # App cleanup must never tear down shared XWayland/compositor state or
    # generic-runtime services.  The old unbounded pkill -f list killed both,
    # invalidating scenario 02's weston-liveness assertion before xterm ran.
    kill_start = helper.find("\nqdwin_apps_kill() {")
    kill_all_start = helper.find("\nqdwin_apps_kill_all() {")
    if kill_start < 0 or kill_all_start < 0 or kill_start > kill_all_start:
        return fail("app helper lacks targeted cleanup before kill-all wrapper")
    cleanup = helper[kill_start:]
    for forbidden in ("Xwayland", " python3", " java", " SwingDemo"):
        if forbidden in cleanup:
            return fail(f"app cleanup can target shared/runtime process {forbidden!r}")
    for token in (
        "refusing unapproved cleanup target",
        'pgrep -u admin -f "^(/[^[:space:]]*/)?\\${pattern}([[:space:]]|\\$)"',
        "kill -TERM --",
        "kill -KILL --",
    ):
        if token not in cleanup:
            return fail(f"app cleanup lacks scoped lifecycle token {token!r}")
    if "qdwin_apps_kill xterm" not in xterm:
        return fail("xterm crash scenario does not use xterm-scoped cleanup")
    if "qdwin_apps_kill_all" in xterm:
        return fail("xterm crash scenario still invokes global app cleanup")
    for token in (
        "pgrep -u admin -x weston | head -1",
        "expected numeric weston PID before xterm",
        "targeted setup cleanup restarted weston",
        "weston restarted (pid $WESTON_PID_BEFORE",
        "exit 1",
    ):
        if token not in xterm:
            return fail(f"xterm crash scenario lacks fail-closed PID token {token!r}")

    # weston_log() embeds its own timestamp in MESSAGE, even with journalctl
    # -o cat. The shell handoff poll must accept that real prefix while keeping
    # the terminal event anchored so an adjacent diagnostic cannot satisfy it.
    handoff_pattern = (
        r"^(\[[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}\] )?"
        r"qdwin: shell unbound$"
    )
    shell_filter = (
        "grep -qE '^(\\[[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{3}\\] )?"
        "qdwin: shell unbound$'"
    )
    if shell_filter not in helper:
        return fail("shell handoff poll does not use the anchored, weston-"
                    "timestamp-tolerant filter")
    positives = (
        "qdwin: shell unbound",
        "[18:27:17.713] qdwin: shell unbound",
    )
    negatives = (
        "noise] qdwin: shell unbound",
        "prefix [foo] qdwin: shell unbound",
        "[8:27:17.713] qdwin: shell unbound",
        "[18:27:17.71] qdwin: shell unbound",
        "[18:27:17x713] qdwin: shell unbound",
        "[18:27:17.713] qdwin: shell unbound late",
    )
    rejected = [message for message in positives
                if re.fullmatch(handoff_pattern, message) is None]
    accepted = [message for message in negatives
                if re.fullmatch(handoff_pattern, message) is not None]
    if rejected:
        return fail(f"shell handoff filter rejects valid message(s): {rejected}")
    if accepted:
        return fail(f"shell handoff filter accepts invalid message(s): {accepted}")

    # Bound the slice at the function's own terminator. Slicing to EOF let a
    # token or a deadline in ANY later helper satisfy these assertions, so a
    # restore path that lost the construct would still pass by aliasing.
    _restore_start = helper.find("\nqdwin_apps_restore_shell() {")
    _restore_end = helper.find("\nEOSCRIPT\n)", _restore_start)
    if _restore_start < 0 or _restore_end < 0:
        return fail("cannot locate qdwin_apps_restore_shell guest script")
    restore = helper[_restore_start:_restore_end]
    for token in (
        "restore: qdshell already owns compositor shell role pid=$qpid",
        # Ownership is proven by UNIT MEMBERSHIP, not MainPID equality:
        # qdshell.service runs `dbus-run-session -- qs`, so MainPID is the
        # wrapper while qdwin logs the `qs` CLIENT pid. MainPID is kept only as
        # a fallback for a hypothetical unwrapped ExecStart; the cgroup check is
        # what actually decides. See qdistro/deploy/qdshell.service.
        "systemctl --user show qdshell.service -p MainPID --value",
        "systemctl --user show qdshell.service -p ControlGroup --value",
        'grep -qx "$_p" "/sys/fs/cgroup$_cg/cgroup.procs"',
        "latest_bound_pid",
        # Pin the CONJUNCTIONS, not just the call names: `pid_is_qdshell "$q" ||
        # true` keeps every bare name token satisfied while accepting anyone.
        'if pid_is_qdshell "$qpid"; then',
        '&& pid_is_qdshell "$qpid"; then',
        "had_bystander=0",
        'grep -qE \'^(\\[[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{3}\\] )?qdwin: shell unbound$\'',
        "systemctl --user start qdshell.service",
        'systemctl --user is-active --quiet qdshell.service',
    ):
        if token not in restore:
            return fail(f"shell restoration lacks compositor handoff token {token!r}")
    had_pos = restore.index("had_bystander=0")
    unbound_pos = restore.index("shell unbound$", had_pos)
    start_pos = restore.index("systemctl --user start qdshell.service")
    # The post-start bind is proven by a CURSOR-SCOPED lifecycle lookup whose
    # pid must then be shown to belong to qdshell.service. The literal
    # "shell bound" text now lives only in the shared regex at the top of the
    # guest script, so anchor the ordering on those two calls instead.
    bound_pos = restore.index('latest_bound_pid --after-cursor "$cursor"',
                              start_pos)
    owned_pos = restore.index('pid_is_qdshell "$qpid"', bound_pos)
    if not (unbound_pos < start_pos < bound_pos < owned_pos):
        return fail("shell restoration does not release-before-start-before-bind")
    # `bound=1` must be reached THROUGH the ownership conjunction above.
    if restore.index("bound=1", owned_pos) < owned_pos:
        return fail("shell restoration sets bound=1 outside the ownership check")
    # The bind wait must be bounded by WALL CLOCK, not an iteration count: each
    # iteration makes several runuser round trips into the guest, so `seq 1 N`
    # bounds the sleeps but not the elapsed time and collapsed to ~6s of real
    # budget under 8-way starvation. Pin the SHAPE and a floor on the budget,
    # not an exact constant, and require the deadline to actually be CONSULTED
    # -- a revert that leaves a dead `_deadline=` assignment behind must fail.
    bind_region = restore[start_pos:]
    deadline = re.search(r"_deadline=\$\(\(SECONDS \+ ([0-9]+)\)\)", bind_region)
    if deadline is None or int(deadline.group(1)) < 30:
        return fail("shell restoration bind wait is not wall-clock bounded "
                    "with at least a 30s budget")
    if '[ "$SECONDS" -ge "$_deadline" ]' not in bind_region:
        return fail("shell restoration bind wait never consults its deadline")
    # Strip comment lines first: the helper's own rationale MENTIONS `seq 1 N`
    # as the shape it replaced, and a naive substring test would read that
    # explanation as the defect it warns about.
    bind_code = "\n".join(line for line in bind_region.splitlines()
                          if not line.lstrip().startswith("#"))
    if "$(seq " in bind_code:
        return fail("shell restoration bind wait is iteration-counted")
    # The bind loop must ALSO require the unit to be active, not merely inherit
    # the fast path's is-active check from earlier in the script.
    if "systemctl --user is-active --quiet qdshell.service" not in bind_region:
        return fail("shell restoration bind wait does not require an active unit")

    # Behavioral truth table for the already-restored fast path. Active alone
    # is insufficient: the latest compositor lifecycle record must be a bound
    # record naming a pid that BELONGS TO qdshell.service. Membership, not
    # MainPID equality, is the ownership test -- the unit runs
    # `dbus-run-session -- qs`, so MainPID is the wrapper and never equals the
    # `qs` client pid qdwin logs; comparing against it made this fast path a
    # guaranteed false negative. This keeps a second EXIT-trap restore harmless
    # without accepting stale, foreign, or bystander-held ownership.
    # The lifecycle filter is the HELPER's own SHELL_LINE_RE, extracted rather
    # than copied: a private copy would let the helper's anchors rot (dropping
    # `^`, or loosening `uid=1000` to `uid=[0-9]+`) while this table still
    # passed against the stale duplicate. The POSIX ERE the shell uses is valid
    # Python `re` as written.
    line_re = re.search(r"^SHELL_LINE_RE='(.*)'$", restore, re.M)
    if line_re is None:
        return fail("shell restoration has no extractable SHELL_LINE_RE")
    shell_line_re = line_re.group(1)
    for required in ("^(", "uid=1000 pid=[0-9]+", "replaying [0-9]+ toplevels", ")$"):
        if required not in shell_line_re:
            return fail(f"SHELL_LINE_RE lost its anchor/identity pin: {required!r}")

    def already_bound(active, main_pid, cgroup_pids, latest):
        if not active:
            return False
        # Mirrors the shell exactly: filter through SHELL_LINE_RE, then take
        # the pid only from a BOUND record (the sed prints nothing on unbind).
        if re.fullmatch(shell_line_re, latest) is None:
            return False
        match = re.search(r"bound \(uid=1000 pid=([0-9]+)\)", latest)
        if match is None:
            return False
        pid = int(match.group(1))
        if pid <= 0:
            return False
        # MainPID fallback first (unwrapped ExecStart), then unit membership.
        return pid == main_pid or pid in cgroup_pids

    # The realistic shape: MainPID is the dbus-run-session wrapper (3681) and
    # the bound pid is the `qs` child (3688). Only the cgroup proves ownership.
    WRAPPER, QS, FOREIGN = 3681, 3688, 4100
    bound_qs = f"qdwin: shell bound (uid=1000 pid={QS}); replaying 0 toplevels"
    positives = (
        (True, WRAPPER, {WRAPPER, QS}, bound_qs),
        (True, WRAPPER, {WRAPPER, QS},
         f"[19:00:13.073] qdwin: shell bound (uid=1000 pid={QS}); replaying 4 toplevels"),
        # Unwrapped ExecStart: MainPID IS the client, cgroup unreadable.
        (True, QS, set(), bound_qs),
        # Wrapped unit whose MainPID is momentarily unreadable: membership in
        # the live cgroup still proves ownership, as it does in the shell.
        (True, 0, {QS}, bound_qs),
    )
    negatives = (
        (False, WRAPPER, {WRAPPER, QS}, bound_qs),            # unit inactive
        # A genuine bystander: some OTHER client holds the role and its pid is
        # absent from qdshell.service's cgroup.
        (True, WRAPPER, {WRAPPER, QS},
         f"qdwin: shell bound (uid=1000 pid={FOREIGN}); replaying 0 toplevels"),
        (True, 0, set(), bound_qs),                           # no MainPID, no cgroup
        (True, WRAPPER, {WRAPPER, QS}, "qdwin: shell unbound"),  # nobody holds the role
        # Anchor cases: unprefixed noise and a trailing-garbage line are not
        # lifecycle records at all, so they can never be the selected record.
        (True, WRAPPER, {WRAPPER, QS},
         f"prefix qdwin: shell bound (uid=1000 pid={QS}); replaying 0 toplevels"),
        (True, WRAPPER, {WRAPPER, QS}, bound_qs + " late"),
        # Identity: a bind by a DIFFERENT uid is not our shell, even when the
        # pid would otherwise pass the ownership test.
        (True, WRAPPER, {WRAPPER, QS},
         f"qdwin: shell bound (uid=0 pid={QS}); replaying 0 toplevels"),
        (True, WRAPPER, {WRAPPER, 0}, "qdwin: shell bound (uid=1000 pid=0); replaying 0 toplevels"),
    )
    if not all(already_bound(*case) for case in positives):
        return fail("already-bound restore model rejects a valid terminal state")
    if any(already_bound(*case) for case in negatives):
        return fail("already-bound restore model accepts inactive/stale ownership")

    credential_tokens = (
        "for _i in $(seq 1 30)",
        "grep -q '^RDP_PASSWORD='",
        "credentials did not arrive within 6 seconds",
        'eval "$CREDS"',
        "RDP_AUTH_CURSOR=$(qdwin_apps_journal_cursor)",
        'qdwin_apps_log_since_cursor "$RDP_AUTH_CURSOR"',
        'pid=$FORWARD_PID reason=\\"forward exited\\"',
        'pid=$SECOND_FORWARD_PID reason=\\"forward exited\\"',
        "ALL_TORN_COUNT",
        "SECOND_ALL_TORN_COUNT",
        "FAIL: qdshell restoration did not reach compositor-bound state",
    )
    missing = [token for token in credential_tokens if token not in rdp]
    if missing:
        return fail(f"RDP scenario lacks bounded credential poll: {missing}")
    if "Step 5 — disconnect cleanup" in rdp:
        return fail("RDP scenario still overclaims subscriber-disconnect coverage")
    restore_call = rdp.rfind("qdwin_apps_restore_shell ||")
    trap_clear = rdp.rfind("trap - EXIT")
    if restore_call < 0 or trap_clear < 0 or restore_call > trap_clear:
        return fail("RDP cleanup disarms EXIT trap before checked shell restore")

    print("PASS: GUI input uses real chords; shell takeover/restoration and "
          "RDP credential collection use bounded observable transitions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
