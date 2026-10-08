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
  weston weston-devel libweston-16 libweston-16-0
  freerdp freerdp-sdl freerdp-server freerdp-devel winpr-devel
  libpixman-1-0 libpixman-1-0-devel
  pipewire wireplumber pipewire-tools pipewire-devel libpipewire-0_3-0
  gstreamer gstreamer-plugin-pipewire gstreamer-plugins-good gstreamer-utils
  meson ninja gcc gcc-c++ pkgconf-pkg-config
  wayland-devel wayland-protocols-devel libxkbcommon-devel libevdev-devel
  libinput-devel libgbm-devel libdrm-devel seatd-devel
  libXcursor-devel adwaita-icon-theme xcursor-themes
  # Devel headers for building the production profile of qdistro's
  # vendored libweston-16 (libweston-vendored/build-libweston.sh
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
  python314-pywayland python314-cffi python314-PyQt6
  qt6-wayland qt6-declarative-imports python314-setuptools python314-pip
  tesseract-ocr grim
  socat Mesa Mesa-libEGL1 Mesa-libGL1 Mesa-dri
  Mesa-demo-egl wayland-utils
  python314-python-pam python314-six fprintd
  python314-dbus-python python314-gobject python314-gobject-Gdk
  python314-PyYAML
  python314-cryptography
  # Textual admin TUI (qdistro-admin-tui; install-admin-cli-for-vm.sh)
  python314-textual python314-rich
  tpm2.0-tools
  sqlite3
  libselinux-devel selinux-policy-devel
  audit
  libnotify-tools
  greetd
  podman passt fuse-overlayfs crun
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
  # qdshell runtime is the vendored upstream Quickshell build
  # (qdshell/quickshell-vendored/, built by the bootstrap's
  # build_quickshell step). No `quickshell` package exists in Tumbleweed —
  # the old bare name here could never resolve; the archived noctalia-qs
  # fork was the only prebuilt provider. First line: the binary's
  # link-time deps; second/third: what its cmake build needs.
  libjemalloc2 libcpptrace1 libpolkit-agent-1-0 libpolkit-gobject-1-0
  cmake spirv-tools vulkan-devel pam-devel polkit-devel jemalloc-devel
  cpptrace-devel qt6-quick-private-devel qt6-qml-private-devel
  qt6-shadertools-devel qt6-waylandclient-devel
  qt6-waylandclient-private-devel
  python314-dbus_next  # qdlocker runtime dep
  # NOTE: python314-PyQt6-WebEngine (qdbrowser WebEngine) is intentionally
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

# --- executed mode ---------------------------------------------------------
# J25: GPG checking is profile-gated. `dev` (disposable VM) may skip signature
# checks for stale/local mirrors; daily-driver/release MUST verify repo
# signatures — an unsigned/tampered mirror could ship a malicious root
# package. The flag array is empty in hardened profiles so zypper's default
# (gpg-checks ON) applies.
_ID_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=lib/qdistro-profile.sh
. "$_ID_DIR/lib/qdistro-profile.sh"
resolve_profile || exit 2

gpg_flags=()
if is_dev; then
    gpg_flags=( --no-gpg-checks )
    echo "[install-deps] WARN: dev profile: zypper --no-gpg-checks (unsigned repo metadata accepted) — NOT a release default" >&2
fi

# J25: don't `|| true`-swallow a refresh failure. In hardened profiles a failed
# refresh (which includes a signature failure) is fatal unless the operator
# explicitly opts into stale cached metadata; dev proceeds on cache with a warn.
if ! zypper -n "${gpg_flags[@]}" refresh; then
    if is_dev || [ "${QDISTRO_ALLOW_STALE_ZYPPER_METADATA:-0}" = 1 ]; then
        echo "[install-deps] WARN: zypper refresh failed; proceeding on cached metadata (install may fail)" >&2
    else
        echo "[install-deps] ERROR: zypper refresh failed in '$QDISTRO_PROFILE' profile (set QDISTRO_ALLOW_STALE_ZYPPER_METADATA=1 to override)" >&2
        exit 1
    fi
fi
# qdshell's runtime is vendored Quickshell (qdshell/quickshell-vendored).
# noctalia-qs was never a legitimate qdistro dep — qdshell forked Noctalia
# before that fork existed — so lock it BEFORE the main transaction: no
# recommends/supplements may pull the archived fork back onto the system.
zypper -n addlock noctalia-qs \
    || { echo "[install-deps] ERROR: zypper addlock noctalia-qs failed" >&2; exit 1; }
zypper -n install --no-recommends "${QDISTRO_PKGS[@]}" 2>&1 | tail -10

# python3 → 3.14: the snapshot still ships the unversioned symlink from
# python313-base while the dep set is python314-* (see lib/qdistro-python.sh).
# shellcheck source=lib/qdistro-python.sh
. "$_ID_DIR/lib/qdistro-python.sh"
ensure_python3_314 || { echo "[install-deps] ERROR: cannot pin /usr/bin/python3 to python3.14" >&2; exit 1; }
echo "[install-deps] DONE"
