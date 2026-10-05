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
#   pres_window_handle <name>   qdwin toplevel handle of an app's window
#   pres_focus_app <name>       focus (not raise) an app's window
#   pres_click_keep_changes     click "Keep changes" in the display confirm
#                               dialog (call right after Apply)
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

# pres_focus_app <name> — FOCUS the app's newest window (qdwin focusWindow).
# It does not necessarily raise it above other windows; scenarios must not
# rely on it to bring a covered window to the front.
pres_focus_app() {
    local handle
    handle=$(pres_window_handle "$1") || return 1
    [ -n "$handle" ] || { echo "pres_focus_app: no toplevel handle for $1" >&2; return 1; }
    pres_qs_ipc qdwin focusWindow "$handle" >/dev/null
}

# pres_click_keep_changes — click "Keep changes" in qdshell's display
# confirm dialog within its 15 s window. A vision driver's look-then-click
# round trip is too slow for that window, so this takes ONE attested frame,
# locates the dialog's filled mPrimary button (mPrimary read from the live
# snapshot; selection rule in the Python below), and clicks its centre
# through the normal QMP pointer path. Call it right after Apply, with the
# settings panel scrolled to the bottom (its filled Layout tab scrolled
# away). Prints the frame path and click point; returns 1 if no
# button-shaped primary region was found.
pres_click_keep_changes() {
    local frame=${TMPDIR:-/tmp}/pres-keep-$$.png primary pt
    primary=$(qdwin_vmx_merged "jq -r .colors.mPrimary $PRES_SNAPSHOT" | tail -n 1)
    qdwin_screenshot "$frame" >/dev/null 2>&1 || { echo "pres_click_keep_changes: capture failed" >&2; return 1; }
    pt=$(python3 - "$frame" "$primary" "${QDWIN_SCREEN_W:-1280}" "${QDWIN_SCREEN_H:-800}" <<'PY'
import sys
from PIL import Image
im = Image.open(sys.argv[1]).convert("RGB")
w, h = im.size
target = tuple(int(sys.argv[2][i:i + 2], 16) for i in (1, 3, 5))
px = im.load()
def close(c):
    return sum(abs(a - b) for a, b in zip(c, target)) <= 24
# Every connected primary-coloured region in the central band is
# flood-filled. The button is the region that is SOLID (>= 60% of its
# bounding box), WIDE (>= 2.5x as wide as tall; toggles are squat) and
# big enough to be a control; the dialog's primary border is a hollow
# frame and fails the solidity test. The largest such region wins.
x0, x1, y0, y1 = w // 4, w, h // 6, 5 * h // 6
seen = bytearray(w * h)
best = None
for y in range(y0, y1, 2):
    for x in range(x0, x1, 2):
        if seen[y * w + x] or not close(px[x, y]):
            continue
        stack = [(x, y)]
        seen[y * w + x] = 1
        n = 0
        bx0 = bx1 = x
        by0 = by1 = y
        while stack:
            cx, cy = stack.pop()
            n += 1
            bx0, bx1 = min(bx0, cx), max(bx1, cx)
            by0, by1 = min(by0, cy), max(by1, cy)
            for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                if x0 <= nx < x1 and y0 <= ny < y1 and not seen[ny * w + nx] and close(px[nx, ny]):
                    seen[ny * w + nx] = 1
                    stack.append((nx, ny))
        bw, bh = bx1 - bx0 + 1, by1 - by0 + 1
        if bh >= h // 60 and bw >= 2.5 * bh and n >= 0.6 * bw * bh:
            if best is None or n > best[0]:
                best = (n, (bx0 + bx1) // 2, (by0 + by1) // 2)
if best is None:
    raise SystemExit(1)
_, cx, cy = best
# qdwin_click takes coordinates in the helper space (QDWIN_SCREEN_W x _H,
# 1280x800 by default), each axis scaled independently.
sw, sh = int(sys.argv[3]), int(sys.argv[4])
print(round(cx * sw / w), round(cy * sh / h), cx, cy)
PY
) || { echo "pres_click_keep_changes: no primary button found in $frame" >&2; return 1; }
    set -- $pt
    qdwin_mouse_move "$1" "$2" >/dev/null 2>&1
    sleep 0.3
    qdwin_click "$1" "$2" >/dev/null 2>&1
    echo "keep-click frame=$frame at frame-px=$3,$4 helper=$1,$2"
}
