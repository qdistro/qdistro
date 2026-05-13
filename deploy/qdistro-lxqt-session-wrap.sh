#!/bin/sh
# Synchronous wrapper passed to labwc's -S option. Updates the DBus
# activation environment with the live Wayland session env BEFORE
# launching lxqt-session, so lxqt-session doesn't pop the
# "DBus Activation Environment wasn't updated" warning dialog.
dbus-update-activation-environment --systemd --all
systemctl --user import-environment WAYLAND_DISPLAY DISPLAY XDG_SESSION_TYPE XDG_CURRENT_DESKTOP 2>/dev/null
exec lxqt-session
