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
  # Devel headers for building the production profile of qdistro's
  # vendored libweston-14 (libweston-vendored/build-libweston.sh
  # QDWIN_LIBWESTON_PROFILE=production): GL renderer (Mesa EGL/GLES),
  # colour management (lcms2), DRM-backend display-info, and the X11
  # backend client libs. Runtime Mesa-libEGL1/GL1 above are not enough
  # to compile the renderer.
  Mesa-libEGL-devel Mesa-libGLESv2-devel Mesa-libGLESv3-devel liblcms2-devel
  libdisplay-info-devel libX11-devel libxcb-devel
  # The shared "toytoolkit" lib (libweston shared/meson.build) is built
  # unconditionally and hard-requires cairo + libpng (+ pango/pangocairo/
  # fontconfig/glib for HAVE_PANGO frame text); the drm backend's VA-API
  # screencast recorder needs libva. Without these, `meson setup` fails with
  # "Dependency not found" before any backend is built — independent of the
  # GL/RDP/pipewire backends above. (libpng16-compat-devel provides the
  # unversioned libpng.pc that dependency('libpng') resolves.)
  cairo-devel libpng16-devel libpng16-compat-devel pango-devel
  fontconfig-devel glib2-devel libva-devel
  python313-pywayland python313-cffi python313-PyQt6
  qt6-wayland qt6-declarative-imports python313-setuptools
  tesseract-ocr grim
  socat Mesa Mesa-libEGL1 Mesa-libGL1 Mesa-dri
  Mesa-demo-egl wayland-utils
  python313-python-pam python313-six fprintd
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
  # Per-silo netns egress (todo/fable-networking task 3 + Opt 3-A): the
  # session-manager's egress backend shells out to wg (wireguard-tools) for
  # wg: tunnels, nft (nftables) for the per-silo backstop + NAT +
  # forward/input isolation, and dnsmasq for the `direct`-egress per-silo
  # resolver. wireguard kernel support is in the Tumbleweed default kernel.
  wireguard-tools nftables dnsmasq
  waypipe
  wl-clipboard
  libvirt libvirt-daemon-qemu libvirt-client virt-install
  qemu-x86 qemu-tools
  qemu-audio-pipewire qemu-audio-alsa
  libguestfs guestfs-tools
  snapper            # btrfs snapshot management
  btrfs-progs        # btrfs subvolume commands
  quickshell         # qdshell runtime
  python313-dbus_next  # qdlocker runtime dep
  # NOTE: python313-PyQt6-WebEngine (qdbrowser WebEngine) is intentionally
  # omitted here because its exact package name is uncertain on Tumbleweed.
  # qdistro-bootstrap.sh tries multiple candidate names with a best-effort
  # (non-fatal) install. To check: zypper search qt6 webengine python
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
