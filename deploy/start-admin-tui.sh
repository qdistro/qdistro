#!/bin/bash
# Launch qterminal inside the uid-1000 admin's running Wayland session
# with the qdistro admin TUI as its only child process. Mirrors the env
# plumbing of start-admin-app.sh so scenarios don't have to set
# XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS / WAYLAND_DISPLAY
# themselves.
#
# Self-elevating: if invoked as root (e.g. directly via `vm-exec`),
# re-exec under the uid-1000 user via runuser so the spawned TUI
# connects to the broker with the admin uid the broker enforces
# (DecideRequest is restricted to uid 1000). Why this matters: a
# common test mistake is to call the launcher from a root shell and
# see the TUI come up but every approve/deny rejected with
# `DecideRequest restricted to admin uid 1000; got 0`. Detecting the
# uid here keeps the scenario robust regardless of whether the OS
# account is named `admin` or `jan`.
set -u
if [ "$(id -u)" = "0" ]; then
    ADMIN_USER=$(getent passwd 1000 | cut -d: -f1)
    if [ -z "$ADMIN_USER" ]; then
        echo "start-admin-tui: no uid-1000 user on this VM" >&2
        exit 2
    fi
    exec runuser -u "$ADMIN_USER" -- "$0" "$@"
fi
export XDG_RUNTIME_DIR=/run/user/1000
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    if [ -S "$XDG_RUNTIME_DIR/wayland-0" ]; then
        export WAYLAND_DISPLAY=wayland-0
    elif [ -S "$XDG_RUNTIME_DIR/wayland-1" ]; then
        export WAYLAND_DISPLAY=wayland-1
    fi
fi
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
export XDG_SESSION_TYPE=wayland
export XDG_CURRENT_DESKTOP=${XDG_CURRENT_DESKTOP:-qdistro}
export DISPLAY=${DISPLAY:-:0}
ADMIN_HOME=$(getent passwd "$(id -u)" | cut -d: -f6)

# Launch log destination.
#
# NEVER a fixed, predictable path in world-writable /tmp. Any other local
# user -- or a stale root-owned file baked into the image -- can pre-create
# that path with ownership this unprivileged user cannot truncate, and the
# redirection below then fails with `Permission denied` before the program
# ever starts: a real denial of service on the supported launcher, plus the
# classic /tmp symlink hazard.
#
# The XDG state directory is used ONLY when it is provably ours: a real
# directory (never a symlink), owned by this uid, and mode 0700 with the
# chmod verified instead of assumed. Once that holds, no other uid can
# create, remove or swap an entry inside it, so a planted pathname cannot
# redirect the log open -- the directory guarantee is what closes the
# file-level race, not a pre-open `[ -L ]` peek.
#
# The log is then opened exactly once, under `noclobber`, which makes bash
# use O_CREAT|O_EXCL: it refuses an existing file *or symlink* rather than
# following/truncating it. Callers write to the resulting fd 9, not to the
# pathname, so there is no second resolution to race. Anything that does not
# hold falls back to a private mktemp file and finally /dev/null; logging
# must never keep the launcher from starting.
#
# Opens fd 9 for the log; callers redirect the child to `>&9`.
qdistro_open_launch_log() {
    local name=$1 base dir file uid old_umask homebase
    uid=$(id -u)
    old_umask=$(umask)
    umask 077
    homebase=
    [ -n "${ADMIN_HOME:-}" ] && homebase="$ADMIN_HOME/.local/state"
    # $XDG_STATE_HOME is honoured only when absolute (a relative value is
    # meaningless to a launcher whose cwd is whatever vm-exec left behind).
    for base in "${XDG_STATE_HOME:-}" "$homebase"; do
        case "$base" in /*) ;; *) continue ;; esac
        dir="$base/qdistro"
        mkdir -p -- "$base" 2>/dev/null
        mkdir -- "$dir" 2>/dev/null
        # Acceptance predicate: real directory, not a symlink, owned by us,
        # and 0700 -- with the chmod required to succeed and re-read back.
        [ -L "$dir" ] && continue
        [ -d "$dir" ] || continue
        chmod 0700 -- "$dir" 2>/dev/null || continue
        [ "$(stat -c '%F|%u|%a' -- "$dir" 2>/dev/null)" = "directory|$uid|700" ] || continue
        file="$dir/$name"
        # Drop any previous log (or symlink) by name, then create fresh under
        # O_EXCL so nothing existing is ever opened.
        rm -f -- "$file" 2>/dev/null || continue
        set -C
        if { exec 9>"$file"; } 2>/dev/null; then
            set +C
            umask "$old_umask"
            return 0
        fi
        set +C
    done
    file=$(mktemp -t "${name%.log}.XXXXXXXX.log" 2>/dev/null)
    if [ -z "$file" ] || ! { exec 9>"$file"; } 2>/dev/null; then
        file=/dev/null
        { exec 9>/dev/null; } 2>/dev/null || true
    fi
    umask "$old_umask"
    echo "${0##*/}: no private state dir for $name; logging to $file" >&2
    return 0
}
qdistro_open_launch_log qterminal-tui.log
setsid qterminal -e /usr/local/bin/qdistro-admin-tui \
    </dev/null >&9 2>&1 &
disown
echo "$!"
