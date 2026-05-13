#!/bin/sh
# qdistro override of /usr/bin/startlxqtwayland: pins labwc and uses a
# wrapper for lxqt-session that pre-populates the DBus activation env.
# Symlinked or copied as /usr/local/bin/startlxqtwayland (preceded
# /usr/bin in PATH because greetd's default_session.command resolves
# via standard PATH).

if [ -z "$XDG_DATA_HOME" ]; then export XDG_DATA_HOME="$HOME/.local/share"; fi
if [ -z "$XDG_CONFIG_HOME" ]; then export XDG_CONFIG_HOME="$HOME/.config"; fi
if [ -z "$XDG_DATA_DIRS" ]; then export XDG_DATA_DIRS="$XDG_DATA_HOME:/usr/local/share:/usr/share"; fi
if [ -z "$XDG_CONFIG_DIRS" ]; then export XDG_CONFIG_DIRS="/etc:/etc/xdg:/usr/share"; fi
if [ -z "$XDG_CACHE_HOME" ]; then export XDG_CACHE_HOME="$HOME/.cache"; fi

mkdir -p "${XDG_DESKTOP_DIR:-$HOME/Desktop}"

if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    if [ -z "$XDG_RUNTIME_DIR" ] || ! [ -S "$XDG_RUNTIME_DIR/bus" ] || ! [ -O "$XDG_RUNTIME_DIR/bus" ]; then
        eval "$(dbus-launch --sh-syntax --exit-with-session)" || echo "qdistro-startlxqtwayland: dbus-launch failed" >&2
    fi
fi

export QT_QPA_PLATFORMTHEME=lxqt
export QT_EXCLUDE_GENERIC_BEARER=1
export QT_AUTO_SCREEN_SCALE_FACTOR=0
export QT_ACCESSIBILITY=1
export XDG_MENU_PREFIX=lxqt-
export XDG_SESSION_TYPE=wayland
export XDG_CURRENT_DESKTOP="LXQt:wlroots"

# VM-friendly cursor handling
if type systemd-detect-virt >/dev/null 2>&1 && systemd-detect-virt --quiet; then
    export WLR_NO_HARDWARE_CURSORS=1
fi

# Make sure labwc config dir exists with LXQt defaults
if [ ! -d "$XDG_CONFIG_HOME/lxqt/labwc" ]; then
    mkdir -p "$XDG_CONFIG_HOME/lxqt"
    cp -av /usr/share/lxqt/wayland/labwc "$XDG_CONFIG_HOME/lxqt/labwc"
fi

export LABWC_CONFIG_DIR="$XDG_CONFIG_HOME/lxqt/labwc"

exec labwc -C "$LABWC_CONFIG_DIR" -S /usr/local/bin/qdistro-lxqt-session-wrap
