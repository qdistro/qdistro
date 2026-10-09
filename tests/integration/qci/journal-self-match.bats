#!/usr/bin/env bats
#
# Static guard for the qemu-ga journal self-match defect class: qemu-ga
# logs every guest-exec command line ('guest-exec called: "/bin/sh -c
# ..."'), so an unscoped `journalctl | grep` inside a guest command can
# match its own command text and pass on a pattern that never existed in
# the product's logs (qdwin-noctalia/06 flake, 2026-09-22;
# qdwin-taskbar-isolation.bats:87 had the same shape).
#
# Host-only static check — no VM. Asserts:
#   1. every `journalctl ... | grep` in tests/integration/vm/*.bats carries
#      a journald field/unit scoping token, and
#   2. the shared wait_for_journal_line helper drops the exec-audit line.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
}

@test "vm bats: every inline journalctl|grep is field- or unit-scoped" {
    local bad=0 entry file lineno text
    while IFS= read -r entry; do
        file=${entry%%:*}
        lineno=${entry#*:}; lineno=${lineno%%:*}
        text=${entry#*:*:}
        # A journald match filter keeps the qga exec-audit line out of the
        # result: unit (-u/--unit/--user-unit/_SYSTEMD_*UNIT=), identifier
        # (-t/SYSLOG_IDENTIFIER=), or any other field selector (_COMM=,
        # _PID=, _UID=). `-b`/`--since` alone bound time, not the source —
        # the exec line lands inside any time window.
        case "$text" in
            *-u\ *|*-t\ *|*--unit=*|*--user-unit=*|*_SYSTEMD_UNIT=*|*_SYSTEMD_USER_UNIT=*|*_COMM=*|*SYSLOG_IDENTIFIER=*|*_PID=*|*_UID=*) continue ;;
        esac
        printf 'UNSCOPED journal grep: %s:%s: %s\n' "$file" "$lineno" "$text" >&2
        bad=1
    done < <(grep -Hn "journalctl" "$REPO_ROOT"/tests/integration/vm/*.bats | grep -F "|" | grep -w grep)
    [ "$bad" -eq 0 ]
}

@test "wait_for_journal_line filters the qga exec-audit line" {
    grep -qF "grep -vF 'guest-exec called:'" \
        "$REPO_ROOT/tests/integration/vm/helpers.bash" \
        || { echo "wait_for_journal_line lost its guest-exec self-match filter" >&2; return 1; }
}
