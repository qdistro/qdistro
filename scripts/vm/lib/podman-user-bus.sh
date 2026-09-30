#!/bin/bash
# podman-user-bus.sh — give the harness's rootless Podman builds the real user
# session bus.
#
# The GUI gate points DBUS_SESSION_BUS_ADDRESS at unix:path=/dev/null for the
# whole gate (gui_isolate_host_desktop in ci/lib/gates/gui.sh) so visual agents
# cannot reach the host desktop, and it builds its goldens after that. Rootless
# Podman's systemd cgroup manager creates each container's scope through the
# user bus; with the address dead-ended, runc fell back to the system bus and
# every container start failed with "unable to apply cgroup configuration ...
# interactive authentication has not been enabled" (2026-09-30: every GUI-only
# run whose source missed the native Podman cache). The builders are harness,
# not agents -- agents run inside run_agent_command's bwrap, which still hides
# the bus -- so only a dead or empty address is replaced, and only by the
# invoking user's own bus socket when it exists.
qdistro_podman_user_bus() {
    local runtime=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
    case "${DBUS_SESSION_BUS_ADDRESS:-}" in
        ""|unix:path=/dev/null)
            if [ -S "$runtime/bus" ]; then
                export DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"
            fi
            ;;
    esac
}
