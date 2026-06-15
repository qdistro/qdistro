#!/bin/sh
# qdwin-session-launcher — what greetd's [default_session].command
# actually invokes after qdgreeter has authenticated admin.
#
# Replaces the legacy /usr/local/bin/qdistro-startlxqtwayland on tty3
# (the production session is qdwin via qdgreeter).
#
# Flow:
#   1. Boot the per-user systemd manager (lingering admin user) so
#      qdwin-session.target's deps can resolve.
#   2. `systemctl --user start --wait` the target. This blocks until
#      the compositor exits, which is exactly the session lifetime
#      semantics greetd expects from `command`.
#   3. Surface non-zero exits so greetd respawns / falls back rather
#      than silently rebooting into a black screen.

set -eu

# greetd sets XDG_RUNTIME_DIR=/run/user/$(id -u) before exec'ing us
# under the target user. Force the authenticated user's runtime dir so
# greeter-only variables cannot leak into the desktop session.
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export XDG_RUNTIME_DIR
export XDG_CACHE_HOME="${HOME:-/home/$(id -un)}/.cache"

export XDG_SESSION_TYPE=wayland
export XDG_CURRENT_DESKTOP=qdistro
export QT_QPA_PLATFORM=wayland
unset QDGREETER_LOG QDGREETER_RAW_KEYBOARD

log() {
    # All logging goes to journal via greetd's PAM session stderr.
    # Do NOT echo password material here — the only thing this
    # script sees from greetd is the post-auth environment.
    printf 'qdwin-session-launcher: %s\n' "$*" >&2
}

log "starting qdwin-session.target for $(id -un)"

systemctl --user import-environment \
    XDG_RUNTIME_DIR XDG_CACHE_HOME XDG_SESSION_TYPE XDG_CURRENT_DESKTOP \
    XDG_SESSION_ID XDG_VTNR WAYLAND_DISPLAY QT_QPA_PLATFORM HOME USER LOGNAME \
    >/dev/null 2>&1 || true

# Wait briefly for the user systemd manager to be reachable. The
# admin user has linger enabled (`loginctl enable-linger admin` in
# install-qdwin-session-for-vm.sh), so the manager is usually
# already running by the time greetd hands us the session.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if systemctl --user is-system-running >/dev/null 2>&1 \
       || systemctl --user list-units >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

# Start the session target, then keep the greetd session process alive
# for as long as the compositor is alive. `systemctl --wait start` on
# a target only waits for the start job to finish on current systemd;
# it does not provide session-lifetime semantics for greetd.
if ! systemctl --user start qdwin-session.target; then
    log "qdwin-session.target start failed; exiting so greetd can recycle"
    exit 1
fi

while :; do
    state="$(systemctl --user show --property=ActiveState --value qdwin-compositor.service 2>/dev/null || true)"
    case "$state" in
        active|activating|reloading)
            sleep 1
            ;;
        *)
            log "qdwin-compositor.service state is ${state:-unknown}; ending session"
            systemctl --user stop qdwin-session.target >/dev/null 2>&1 || true
            exit 0
            ;;
    esac
done
