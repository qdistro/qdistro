#!/bin/sh
# Point the labwc GUI-test session's swaybg at the patterned test wallpaper.
#
# Usage: labwc-test-wallpaper.sh <labwc-config-dir> <wallpaper.png> [<seed-dir>]
#
# TEST-IMAGE ONLY. Called from spin-test-vm-gui.sh while baking the labwc
# (gui-admin) golden. It never touches the shipped product defaults: it edits
# the admin user's copy of the labwc config, not /usr/share.
#
# WHO STARTS swaybg. Not qdistro. The labwc session is
#   qdistro-labwc.service (admin user unit, spin-test-vm-gui.sh step 8)
#   -> /usr/local/bin/startlxqtwayland (deploy/qdistro-startlxqtwayland.sh)
#   -> labwc -C ~/.config/lxqt/labwc -S qdistro-lxqt-session-wrap
# and labwc runs <config-dir>/autostart at startup. startlxqtwayland seeds
# that directory on first run by copying /usr/share/lxqt/wayland/labwc, which
# the openSUSE lxqt-labwc-session package ships with
#   swaybg -i /usr/share/wallpapers/openSUSEdefault/contents/images/default.png
# That image belongs to the openSUSE branding packages, which the qdistro image
# does not install, so swaybg starts, cannot load its image, and the desktop
# stays solid black. An empty desktop then looks exactly like a dead display
# and vm-gui refuses it as near-black.
#
# WHAT THIS DOES.
#  - Seeds <labwc-config-dir> from <seed-dir> (default the distro directory
#    above) when it does not exist yet, exactly as startlxqtwayland would. The
#    directory then exists, so startlxqtwayland's own seed step is skipped and
#    cannot overwrite the edit.
#  - Removes EVERY uncommented line that runs swaybg (the distro invocation and
#    any earlier copy of ours), so no second background client races ours for
#    the background layer. labwc -C reads autostart only from that directory,
#    so there is no system-wide file that could also start one.
#  - Appends one marked swaybg line in tile mode: tile draws the image
#    unscaled, so on the lane's 1280x800 output the frame outside windows is
#    pixel-identical to the asset, and at any other size it is still the
#    unscaled 64px pattern. A flat colour would not do: vm-gui's
#    screenshot_is_usable refuses a frame with grayscale sigma under 0.01.
#  - Fails if swaybg is not installed, rather than baking a line that can
#    never run.
# Idempotent: a second run leaves the file byte-identical.
set -eu

cfg=${1:?usage: labwc-test-wallpaper.sh <labwc-config-dir> <wallpaper.png> [<seed-dir>]}
wp=${2:?usage: labwc-test-wallpaper.sh <labwc-config-dir> <wallpaper.png> [<seed-dir>]}
seed=${3:-/usr/share/lxqt/wayland/labwc}
marker='# qdistro GUI test lane: patterned test wallpaper (scripts/vm/assets/labwc-test-wallpaper.sh)'

command -v swaybg >/dev/null 2>&1 \
    || { echo "labwc-test-wallpaper: swaybg is not installed" >&2; exit 1; }
[ -s "$wp" ] || { echo "labwc-test-wallpaper: wallpaper $wp is missing or empty" >&2; exit 1; }
case "$wp" in
    *\'*|*' '*) echo "labwc-test-wallpaper: wallpaper path must not contain quotes or spaces" >&2; exit 1 ;;
esac

if [ ! -d "$cfg" ]; then
    mkdir -p "$(dirname "$cfg")"
    if [ -d "$seed" ]; then
        cp -a "$seed" "$cfg"
    else
        mkdir -p "$cfg"
    fi
fi

auto="$cfg/autostart"
[ -f "$auto" ] || : > "$auto"

tmp="$auto.qdistro-tmp"
# Drop our marker and every uncommented swaybg invocation; keep the rest.
awk -v m="$marker" '
    $0 == m { next }
    /^[[:space:]]*#/ { print; next }
    /(^|[^[:alnum:]_-])swaybg([^[:alnum:]_-]|$)/ { next }
    { print }
' "$auto" > "$tmp"
printf '%s\n%s\n' "$marker" \
    "swaybg -m tile -i '$wp' >\"\${XDG_RUNTIME_DIR:-/tmp}/qdistro-swaybg.log\" 2>&1 &" >> "$tmp"
mv -f "$tmp" "$auto"
