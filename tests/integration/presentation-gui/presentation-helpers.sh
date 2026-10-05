#!/bin/bash
# presentation-helpers.sh — host-side helpers for the presentation GUI
# scenarios (tests/integration/presentation-gui/NN-*.md). Sources the qdwin
# helpers (QMP input, attested qdwin_screenshot, qdwin_vmx_merged) and adds:
#
#   pres_admin <cmd>            run <cmd> as admin inside the Wayland session
#                               env (merged output, bounded capture)
#   pres_qs_ipc <args...>       `qs ipc call <args...>` against qdshell
#   pres_snapshot               print "<generation> <mode>" of current.json
#   pres_wait_mode <mode> [s]   wait until current.json mode == <mode>
#   pres_launch <tag> <cmd>     start a GUI app detached in the session
#   pres_app_pids               "name=pid ..." for the four first-party apps
#   pres_kill_apps              stop every first-party app this file starts
#   pres_output_scale           integer wl_output scale of Virtual-1
#
# Usage (from a scenario's Setup block):
#   source "${QDISTRO_REPO}/tests/integration/presentation-gui/presentation-helpers.sh"
#   qdwin_set_vm "${VMNAME:-...}"

: "${QDISTRO_REPO:=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)}"
: "${QDWIN_REPO:=${QDISTRO_REPO}/qdwin}"
: "${QDLOCKER_REPO:=${QDISTRO_REPO}/qdlocker}"
: "${QDWIN_VM_EXEC:=${QDISTRO_REPO}/scripts/vm/vm-exec}"

# shellcheck source=/dev/null
source "${QDWIN_REPO}/tests/gui/qdwin-helpers.sh"

PRES_SESSION_ENV="XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus"
PRES_SNAPSHOT=/var/lib/qdistro/presentation/current.json

pres_admin() {
    # One argument: the guest shell command, run as admin with the session env.
    # Shipped base64-encoded so any quoting inside it survives intact.
    local b64
    b64=$(printf '%s' "$1" | base64 -w0)
    qdwin_vmx_merged "cd / && echo $b64 | base64 -d | runuser -u admin -- env $PRES_SESSION_ENV sh"
}

pres_qs_ipc() {
    qdwin_vmx_merged "runuser -l admin -c 'XDG_RUNTIME_DIR=/run/user/1000 qs -p /usr/share/quickshell/qdshell ipc --any-display call $*'"
}

pres_snapshot() {
    qdwin_vmx_merged "jq -r '.generation + \" \" + .mode' $PRES_SNAPSHOT" | tail -n 1
}

pres_wait_mode() {
    local want=$1 limit=${2:-20} i snap
    for ((i = 0; i < limit * 2; i++)); do
        snap=$(pres_snapshot)
        [ "${snap##* }" = "$want" ] && { printf '%s\n' "$snap"; return 0; }
        sleep 0.5
    done
    printf 'pres_wait_mode: still "%s" after %ss (wanted %s)\n' "$snap" "$limit" "$want" >&2
    return 1
}

# pres_launch <tag> <command...> — detached, logs to /tmp/pres-<tag>.log.
pres_launch() {
    local tag=$1; shift
    qdwin_vmx_merged "cd / && setsid -f runuser -u admin -- env $PRES_SESSION_ENV $* >/tmp/pres-$tag.log 2>&1 < /dev/null"
}

# Process patterns of the four apps as started by these scenarios.
PRES_APP_PATTERNS=(
    "qfileman=[q]fileman"
    "qterminator=[q]terminator"
    "qdbrowser=python3 -m [q]dbrowser"
    "qnotebook=[q]notebook"
)

pres_app_pids() {
    local entry name pat out=""
    for entry in "${PRES_APP_PATTERNS[@]}"; do
        name=${entry%%=*}
        pat=${entry#*=}
        out+="$name=$(qdwin_vmx_merged "pgrep -u admin -f -o '$pat' || echo none" | tail -n 1) "
    done
    printf '%s\n' "${out% }"
}

pres_kill_apps() {
    qdwin_vmx_merged "pkill -u admin -f '[q]fileman|[q]terminator|python3 -m [q]dbrowser|[q]notebook|[q]distro-polkit-prompt' ; sleep 1; true" >/dev/null
}

pres_output_scale() {
    pres_admin "wayland-info 2>/dev/null" \
        | awk '/name: Virtual-1/ {found=1} found && /scale:/ {sub(/.*scale: */, ""); sub(/,.*/, ""); print; exit}'
}

# pres_window_handle <name> — qdwin toplevel handle of the app's newest window
# (from the compositor's `toplevel_added handle=N uid=1000 pid=P` journal
# line; same source as qdwin/tests/gui/18-workspace-switch.md).
pres_window_handle() {
    local name=$1 entry pat pid
    for entry in "${PRES_APP_PATTERNS[@]}"; do
        [ "${entry%%=*}" = "$name" ] && pat=${entry#*=}
    done
    [ -n "${pat:-}" ] || { echo "pres_window_handle: unknown app $name" >&2; return 2; }
    pid=$(qdwin_vmx_merged "pgrep -u admin -f -o '$pat'" | tail -n 1)
    [ -n "$pid" ] || return 1
    pres_admin "journalctl --user -u qdwin-compositor.service -b --no-pager 2>/dev/null" \
        | grep -E "qdwin: toplevel_added handle=[0-9]+ uid=1000 pid=$pid( |$)" \
        | tail -n 1 | sed -nE 's/.*handle=([0-9]+).*/\1/p'
}

# pres_focus_app <name> — raise and focus the app's newest window.
pres_focus_app() {
    local handle
    handle=$(pres_window_handle "$1") || return 1
    [ -n "$handle" ] || { echo "pres_focus_app: no toplevel handle for $1" >&2; return 1; }
    pres_qs_ipc qdwin focusWindow "$handle" >/dev/null
}
