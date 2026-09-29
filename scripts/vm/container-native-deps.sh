#!/usr/bin/env bash
# Build a snapshot-pinned rootless Podman layer with native build dependencies.
set -euo pipefail

snap=${QDISTRO_SUBSTRATE_SNAPSHOT:?}
[[ "$snap" =~ ^20[0-9]{6}$ ]] || { echo "invalid snapshot: $snap" >&2; exit 2; }

rm -f /etc/zypp/repos.d/*.repo /etc/zypp/services.d/*.service
cat > /etc/zypp/repos.d/qdistro-snapshot-oss.repo <<EOF
[qdistro-snapshot-oss]
name=qdistro Tumbleweed OSS $snap
enabled=1
autorefresh=0
keeppackages=1
baseurl=https://download.opensuse.org/history/$snap/tumbleweed/repo/oss/
gpgcheck=1
EOF
cat > /etc/zypp/repos.d/qdistro-snapshot-nonoss.repo <<EOF
[qdistro-snapshot-nonoss]
name=qdistro Tumbleweed NonOSS $snap
enabled=1
autorefresh=0
keeppackages=1
baseurl=https://download.opensuse.org/history/$snap/tumbleweed/repo/non-oss/
gpgcheck=1
EOF
zypper -n refresh
zypper -n dup --no-recommends
zypper -n install --no-recommends \
    meson ninja gcc gcc-c++ pkgconf-pkg-config git file make \
    selinux-policy-devel selinux-policy-targeted checkpolicy policycoreutils findutils \
    weston-devel libweston-16 libweston-16-0 \
    wayland-devel wayland-protocols-devel libinput-devel libXcursor-devel \
    freerdp-devel winpr-devel pipewire-devel libselinux-devel \
    qt6-base-devel qt6-core-devel qt6-qml-devel qt6-quick-devel \
    qt6-declarative-devel qt6-svg-devel qt6-shadertools \
    libpixman-1-0-devel libxkbcommon-devel libevdev-devel \
    libgbm-devel libdrm-devel seatd-devel \
    Mesa-libEGL-devel Mesa-libGLESv2-devel Mesa-libGLESv3-devel \
    liblcms2-devel libdisplay-info-devel libX11-devel libxcb-devel \
    cairo-devel libpng16-devel libpng16-compat-devel pango-devel \
    fontconfig-devel glib2-devel libva-devel
