#!/bin/bash
# In-VM helper: make the caller the only writer of the managed presentation
# snapshot while a probe runs.
#
# The session qdshell republishes /var/lib/qdistro/presentation/current.json
# on settings load/save and on colour-scheme changes (AppPresentationService),
# which can land seconds after boot, in the middle of a probe. `hold` waits
# until admin's user manager has no queued start jobs (so nothing starts the
# shell again later), then stops qdshell.service; stopping the unit also
# kills an in-flight qdistro-presentation-publish. `release` starts the shell
# again if hold stopped a running or starting one.
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
        echo "shell-publisher: no session shell installed; nothing to hold"
        return 0
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
    state=$(uctl show -p ActiveState --value "$UNIT" 2>/dev/null)
    case "$state" in
        inactive|failed) : >"$MARK" ;;
        *) echo restart >"$MARK" ;;
    esac
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
    echo "shell-publisher: held ($UNIT was ${state:-unknown} after stop; restart=$(cat "$MARK" 2>/dev/null))"
}

release() {
    [ -f "$MARK" ] || return 0
    if grep -qx restart "$MARK"; then
        rm -f "$MARK"
        uctl start "$UNIT" || { echo "shell-publisher: restarting $UNIT failed" >&2; return 1; }
    else
        rm -f "$MARK"
    fi
}

case "${1:-}" in
    hold) hold ;;
    release) release ;;
    *) echo "usage: $0 hold|release" >&2; exit 2 ;;
esac
