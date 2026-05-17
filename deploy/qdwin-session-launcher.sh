#!/bin/sh
# qdwin-session-launcher — what greetd's [default_session].command
# actually invokes after qdgreeter has authenticated admin.
#
# Replaces the legacy /usr/local/bin/qdistro-startlxqtwayland on
# tty3. The legacy script still exists on tty4 as the escape hatch
# (see deploy/greetd-config.toml).
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
# under the target user. Defensive default in case a future greetd
# version drops the env.
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR

export XDG_SESSION_TYPE=wayland
export XDG_CURRENT_DESKTOP=qdistro
export QT_QPA_PLATFORM=wayland

log() {
    # All logging goes to journal via greetd's PAM session stderr.
    # Do NOT echo password material here — the only thing this
    # script sees from greetd is the post-auth environment.
    printf 'qdwin-session-launcher: %s\n' "$*" >&2
}

log "starting qdwin-session.target for $(id -un)"

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

# --wait blocks until the target stops. If the compositor exits, the
# whole target stops and we return — greetd then puts us back on the
# greeter (a fresh qdgreeter invocation, not a respawn loop).
if ! systemctl --user --wait start qdwin-session.target; then
    log "qdwin-session.target start failed; exiting so greetd can recycle"
    exit 1
fi

exit 0
