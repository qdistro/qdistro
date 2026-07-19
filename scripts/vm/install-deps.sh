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
  # colour management (lcms2), DRM-backend display-info, the X11
  # backend client libs, and weston's shared toytoolkit deps. Without
  # these, the production-profile vendored libweston configure fails and
  # qdwin falls back to stock libweston-16, silently disabling the
  # layer-popup clamp/grab paths.
  Mesa-libEGL-devel Mesa-libGLESv2-devel Mesa-libGLESv3-devel liblcms2-devel
  libdisplay-info-devel libX11-devel libxcb-devel
  cairo-devel libpng16-devel libpng16-compat-devel pango-devel
  fontconfig-devel glib2-devel libva-devel
  python313-pywayland python313-cffi python313-PyQt6
  qt6-wayland python313-setuptools
  socat Mesa Mesa-libEGL1 Mesa-libGL1 Mesa-dri
  Mesa-demo-egl wayland-utils
  python313-python-pam python313-six fprintd
  python313-dbus-python python313-gobject python313-gobject-Gdk
  # jeepney: pure-Python D-Bus used by qdbrowser/pwd_autofill.py +
  # qdistro/browser_bridge/. Without it, the autofill prompt RPC
  # short-circuits to {"ok": false, "reason": "jeepney_missing"}.
  python313-jeepney
  python313-PyYAML
  python313-cryptography
  # rage (Rust age impl): the backup CLI (snapshots/qdistro_backup_cli.py)
  # encrypts every blob through `rage -e | ... | rage -d` ($QDISTRO_RAGE,
  # default "rage"). The package is `rage-encryption` (provides /usr/bin/rage
  # + rage-keygen); without it backups/restores cannot encrypt and the real
  # btrfs backup lane (tests/integration/vm/backup-btrfs-e2e.bats) cannot run.
  rage-encryption
  # rsync: the backup-service driver's metadata collector
  # (snapshots/qdistro_backup_service.py) stages the config-file set with
  # `rsync -aHAX --delete`, and the ssh transport pushes blobs via rsync -e ssh.
  # Without it the daily backup's collector subvol fails.
  rsync
  tpm2.0-tools
  sqlite3
  libselinux-devel selinux-policy-devel
  audit
  libnotify-tools
  greetd
  podman slirp4netns fuse-overlayfs crun
  # Per-silo egress backend: session-manager shells out to `wg`
  # (wireguard-tools) and programs `nft` rules; `dnsmasq` serves the
  # per-netns resolver. Required in the baked base so the silo-egress
  # VM lane (tests/integration/vm/silo-egress.bats) is not a no-op —
  # these previously lived only in scripts/install/install-deps.sh, so
  # baseweed-baked clones lacked them and the lane failed at setup.
  wireguard-tools nftables dnsmasq
  waypipe
  wl-clipboard
  # OCR engine used by qdwin-noctalia/03-clock-updates.md. The scenario
  # verifies the qdshell clock bar with tesseract and skips when the binary
  # is missing, which makes absence from the baked GUI VM a dependency gap.
  tesseract-ocr
  # Test-VM-only synthetic input. Requires a kernel with uinput; production
  # images must not depend on this package.
  ydotool
  # Full kernel for the test VM. The baseweed base derives from the upstream
  # Tumbleweed Minimal-VM Cloud image, which ships only `kernel-default-base`
  # — a stripped kernel that omits less-common modules, including `uinput`
  # (CONFIG_INPUT_UINPUT). Without uinput there is no /dev/uinput and ydotool
  # is a no-op, so the GUI input tests (s33/s60) can't reach hard-pass.
  # Installing `kernel-default` adds the full module set. When this list is
  # baked into baseweed-baked.qcow2 (build-baked-baseweed.sh), the resulting
  # base BOOTS the full kernel from first boot, so clones have /dev/uinput
  # with no reboot. NOTE: `kernel-default` is the SAME package the PRODUCTION
  # image already uses (image/config.xml) — it is test-only HERE only because
  # the Minimal-VM-derived test base started from kernel-default-base;
  # production is unaffected.
  kernel-default
  libvirt libvirt-daemon-qemu libvirt-client virt-install
  qemu-x86 qemu-tools
  qemu-audio-pipewire qemu-audio-alsa
  libguestfs guestfs-tools
  # qdshell QML stack: noctalia-qs is Tumbleweed's quickshell package
  # (the binary is /usr/bin/qs, despite the package being named
  # noctalia-qs after the Noctalia project that ships it).
  noctalia-qs
  qt6-declarative-imports qt6-svg-devel qt6-shadertools
  # Qt6 devel headers — required so fresh-vm-bootstrap.sh's in-VM
  # `meson setup` of qdshell/qml-plugin/ can find Qt6Core / Qt6Qml /
  # Qt6QmlIntegration via pkg-config. Without these, meson aborts with
  # `Dependency "Qt6Core" not found`, which cascades to ~80 bats failures
  # because the QML plugin is a hard prereq for the shell session.
  # Qt6QmlIntegration.pc ships in qt6-qml-devel (not a separate
  # qt6-qmlintegration-devel package on Tumbleweed).
  qt6-base-devel qt6-declarative-devel qt6-qml-devel
  # bats for in-VM integration tests
  bats
)

# When sourced, return without running zypper. Sourceable detection:
# in bash, `${BASH_SOURCE[0]}` differs from `${0}` if we were sourced.
# Keep PKGS exported as a back-compat alias.
PKGS=("${QDISTRO_PKGS[@]}")
if [ "${BASH_SOURCE[0]:-$0}" != "${0}" ]; then
    return 0 2>/dev/null || true
fi

# J25: verify repo signatures (no --no-gpg-checks) and don't swallow a refresh
# failure. This VM-side deps install feeds baseweed builder VMs whose output
# can back a release image build, so it must not accept unsigned metadata.
zypper -n refresh
zypper -n install --no-recommends "${QDISTRO_PKGS[@]}" 2>&1 | tail -10
echo "[install-deps] DONE"
