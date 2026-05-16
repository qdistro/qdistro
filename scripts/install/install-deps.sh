#!/bin/bash
# Idempotent zypper install of all qdwin §6.5/§6.6 build+runtime deps.
# Names are Tumbleweed as of 2026-04-25.
#
# Two modes:
#   - executed:  runs zypper -n install ... + emits "[install-deps] DONE".
#   - sourced:   defines QDISTRO_PKGS array + returns; the caller drives
#                the install (e.g. build-baked-baseweed.sh uses
#                virt-customize at-rest instead of in-VM zypper-via-qga).
set -eo pipefail
QDISTRO_PKGS=(
  weston weston-devel libweston-14 libweston-14-0
  freerdp freerdp-sdl freerdp-server freerdp-devel winpr-devel
  libpixman-1-0 libpixman-1-0-devel
  pipewire wireplumber pipewire-tools pipewire-devel libpipewire-0_3-0
  gstreamer gstreamer-plugin-pipewire gstreamer-plugins-good gstreamer-utils
  meson ninja gcc gcc-c++ pkgconf-pkg-config
  wayland-devel wayland-protocols-devel libxkbcommon-devel libevdev-devel
  libinput-devel libgbm-devel libdrm-devel seatd-devel
  libXcursor-devel adwaita-icon-theme xcursor-themes
  python313-pywayland python313-cffi python313-PyQt6
  python313-pyside6
  qt6-wayland python313-setuptools
  tesseract-ocr grim
  socat Mesa Mesa-libEGL1 Mesa-libGL1 Mesa-dri
  Mesa-demo-egl wayland-utils
  python313-python-pam fprintd
  python313-dbus-python python313-gobject python313-gobject-Gdk
  python313-PyYAML
  python313-cryptography
  tpm2.0-tools
  sqlite3
  libselinux-devel selinux-policy-devel
  audit
  libnotify-tools
  greetd
  podman slirp4netns fuse-overlayfs crun
  waypipe
  wl-clipboard
  libvirt libvirt-daemon-qemu libvirt-client virt-install virt-viewer
  qemu-x86 qemu-tools qemu-ui-spice-core qemu-ui-spice-app spice-vdagent
  qemu-audio-pipewire qemu-audio-alsa
  libguestfs guestfs-tools
)

# When sourced, return without running zypper. Sourceable detection:
# in bash, `${BASH_SOURCE[0]}` differs from `${0}` if we were sourced.
# Keep PKGS exported as a back-compat alias.
PKGS=("${QDISTRO_PKGS[@]}")
if [ "${BASH_SOURCE[0]:-$0}" != "${0}" ]; then
    return 0 2>/dev/null || true
fi

zypper -n --no-gpg-checks refresh >/dev/null 2>&1 || true
zypper -n install --no-recommends "${QDISTRO_PKGS[@]}" 2>&1 | tail -10
echo "[install-deps] DONE"
