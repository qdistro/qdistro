#!/bin/bash
# In-VM helper: make the caller the only writer of the managed presentation
# snapshot while a probe runs.
#
# The session qdshell republishes /var/lib/qdistro/presentation/current.json
# on settings load/save and on colour-scheme changes (AppPresentationService),
# which can land seconds after boot, in the middle of a probe. `hold` waits
# until admin's user manager has no queued start jobs (so nothing starts the
# shell again later), then stops qdshell.service; stopping the unit also
# kills an in-flight qdistro-presentation-publish. Hold records the unit's
# last activation; `release` fails if the shell was activated again while
# the probe ran (a compositor or session-target restart re-pulls it through
# PartOf=), then starts the shell again if hold stopped a running one. The
# presentation bats lanes run a baked desktop session, so a missing shell
# unit is a failure, not an empty hold.
#
#   shell-publisher.sh hold      exit 0 = no other writer; nonzero = refuse to run
#   shell-publisher.sh release
set -u

UNIT=qdshell.service
MARK=/run/qdistro-probe-shell-held
uctl() { systemctl --user -M admin@ "$@"; }

installed() {
    local d
    for d in /home/admin/.config/systemd/user /etc/systemd/user /usr/lib/systemd/user; do
        [ -e "$d/$UNIT" ] && return 0
    done
    return 1
}

hold() {
    local deadline jobs state
    rm -f "$MARK"
    if ! installed; then
        echo "shell-publisher: $UNIT is not installed for admin; the session shell is missing" >&2
        return 1
    fi
    deadline=$((SECONDS + 120))
    while :; do
        # list-jobs fails while the manager is unreachable; empty = settled.
        jobs=$(uctl list-jobs --no-legend 2>/dev/null) && [ -z "$jobs" ] && break
        [ "$SECONDS" -lt "$deadline" ] || {
            echo "shell-publisher: admin's user manager did not settle in 120 s: ${jobs:-unreachable}" >&2
            return 1
        }
        sleep 1
    done
    local restart=no entered
    state=$(uctl show -p ActiveState --value "$UNIT" 2>/dev/null)
    case "$state" in
        inactive|failed) ;;
        *) restart=yes ;;
    esac
    printf 'restart=%s\nentered=\n' "$restart" >"$MARK"
    uctl stop "$UNIT" || { echo "shell-publisher: stopping $UNIT failed" >&2; return 1; }
    state=$(uctl show -p ActiveState --value "$UNIT" 2>/dev/null)
    case "$state" in
        inactive|failed) ;;
        *) echo "shell-publisher: $UNIT is $state after stop" >&2; return 1 ;;
    esac
    if pgrep -u admin -f 'qdistro-presentation-publish' >/dev/null; then
        echo "shell-publisher: a presentation publisher is still running" >&2
        return 1
    fi
    entered=$(uctl show -p ActiveEnterTimestampMonotonic --value "$UNIT" 2>/dev/null)
    [ -n "$entered" ] || { echo "shell-publisher: cannot read $UNIT activation time" >&2; return 1; }
    printf 'restart=%s\nentered=%s\n' "$restart" "$entered" >"$MARK"
    echo "shell-publisher: held ($UNIT $state after stop; was running: $restart)"
}

release() {
    local restart entered now state rc=0
    [ -f "$MARK" ] || return 0
    restart=$(sed -n 's/^restart=//p' "$MARK")
    entered=$(sed -n 's/^entered=//p' "$MARK")
    rm -f "$MARK"
    if [ -n "$entered" ]; then
        state=$(uctl show -p ActiveState --value "$UNIT" 2>/dev/null)
        now=$(uctl show -p ActiveEnterTimestampMonotonic --value "$UNIT" 2>/dev/null)
        if [ "$now" != "$entered" ] || { [ "$state" != inactive ] && [ "$state" != failed ]; }; then
            echo "shell-publisher: $UNIT was activated while held (state $state, activation $entered -> $now); the probe was not the only writer" >&2
            rc=1
        else
            echo "shell-publisher: $UNIT stayed stopped for the whole probe"
        fi
    fi
    if [ "$restart" = yes ]; then
        uctl start "$UNIT" || { echo "shell-publisher: restarting $UNIT failed" >&2; rc=1; }
    fi
    return "$rc"
}

case "${1:-}" in
    hold) hold ;;
    release) release ;;
    *) echo "usage: $0 hold|release" >&2; exit 2 ;;
esac
