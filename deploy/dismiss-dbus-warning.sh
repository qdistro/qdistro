#!/bin/sh
# Workaround: liblxqt pops a "DBus Activation Environment wasn't updated"
# warning dialog at session start that blocks subsequent autostart entries
# until clicked. We can't suppress it at source without patching liblxqt,
# so this helper polls for it and presses Return once.
# Runs from the compositor-session autostart in the background.
export XDG_RUNTIME_DIR=/run/user/1000
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    if [ -S "$XDG_RUNTIME_DIR/wayland-0" ]; then
        export WAYLAND_DISPLAY=wayland-0
    elif [ -S "$XDG_RUNTIME_DIR/wayland-1" ]; then
        export WAYLAND_DISPLAY=wayland-1
    fi
fi
for _ in $(seq 1 30); do
    sleep 1
    # If lxqt-panel is up, the dialog has either been dismissed or never appeared
    if pgrep -x lxqt-panel >/dev/null 2>&1; then
        exit 0
    fi
    # Send Enter; if no focused window or no dialog, harmless
    wtype -k Return 2>/dev/null
done
