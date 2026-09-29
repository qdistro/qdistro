#!/usr/bin/env bash
# Runs as root INSIDE a rootless Podman container. /src is a disposable copy.
set -euo pipefail

snap=${QDISTRO_SUBSTRATE_SNAPSHOT:?}
[[ "$snap" =~ ^20[0-9]{6}$ ]] || { echo "invalid snapshot: $snap" >&2; exit 2; }

# The container's base layer may predate the test cloud image. Align its RPMs
# with the same immutable repository snapshot before compiling anything.
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
    selinux-policy-devel checkpolicy policycoreutils \
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

QDWIN_LIBWESTON_PROFILE=production \
    bash /src/qdwin/libweston-vendored/build-libweston.sh
export PKG_CONFIG_PATH="$(bash /src/qdwin/libweston-vendored/pkgconfig-dir.sh):${PKG_CONFIG_PATH:-}"
DEST=/out/stage/usr/libexec/qdistro/qdwin-libweston \
    bash /src/scripts/install/install-vendored-libweston.sh /src/qdwin

for component in qdwin daemons qdshell; do
    opts=()
    if [ "$component" = qdwin ] && [ -n "${QDWIN_EXTRA_MESON_OPTS:-}" ]; then
        read -r -a opts <<< "$QDWIN_EXTRA_MESON_OPTS"
    fi
    meson setup "/out/build-$component" "/src/$component" --prefix=/usr "${opts[@]}"
    meson compile -C "/out/build-$component"
    # Daemons/qdshell resolve the protocol from the preceding installation.
    meson install -C "/out/build-$component"
    meson install -C "/out/build-$component" --destdir /out/stage
done

# qsu's ELF caller identity is security-relevant; stage the same flags as the
# guest installer instead of falling back to its Python wrapper.
install -d /out/stage/usr/local/bin
cc -O2 -Wall -Wextra -Wformat=2 -Werror=format-security \
    -o /out/stage/usr/local/bin/qsu /src/qsu/qsu.c

# The Python Wayland probes generate bindings at test time. Ship their XML
# inputs without installing the wayland-devel/header packages in the guest.
install -Dm0644 /usr/share/wayland/wayland.xml \
    /out/stage/usr/share/wayland/wayland.xml
cp -a /usr/share/wayland-protocols /out/stage/usr/share/
install -Dm0644 /usr/share/pkgconfig/wayland-protocols.pc \
    /out/stage/usr/share/pkgconfig/wayland-protocols.pc

# Refpolicy modules also require build tools. Compile them against this same
# snapshot and install the packages (not the toolchain) in the guest.
install -d /out/stage/usr/share/qdistro-build/selinux \
    /usr/share/selinux/devel/include/contrib
for policy in pwd broker session_manager tier1; do
    policy_dir=/src/selinux/$policy
    for interface in "$policy_dir"/*.if; do
        [ -f "$interface" ] || continue
        install -m 0644 "$interface" /usr/share/selinux/devel/include/contrib/
    done
    make -C "$policy_dir" "MODULE=qdistro_$policy"
    install -m 0644 "$policy_dir/qdistro_$policy.pp" \
        "/out/stage/usr/share/qdistro-build/selinux/qdistro_$policy.pp"
done

test -s /out/stage/usr/lib64/weston/qdwin-shell.so
test -s /out/stage/usr/share/qdistro/qml/Qdistro/Qdwin/libqdistro-qdwin.so
test -s /out/stage/usr/bin/qdistro-secctx-exec
test -s /out/stage/usr/local/bin/qsu
test -s /out/stage/usr/libexec/qdistro/qdwin-libweston/lib64/libweston-16/drm-backend.so

install -d /out/stage/usr/share/qdistro-build
printf '%s\n' "$snap" > /out/stage/usr/share/qdistro-build/snapshot
rpm -q --qf '%{NAME} %{VERSION}-%{RELEASE}\n' \
    glibc libwayland-client0 libweston-16-0 libQt6Core6 \
    > /out/stage/usr/share/qdistro-build/build-rpms.txt
find /out/stage -type f -print0 | while IFS= read -r -d '' staged_file; do
    if file -b "$staged_file" | grep -Eq '^ELF .* (dynamically linked|shared object)'; then
        printf '%s\n' "${staged_file#/out/stage}"
    fi
done > /out/stage/usr/share/qdistro-build/elf-manifest
test -s /out/stage/usr/share/qdistro-build/elf-manifest
echo "[native-podman] staged $(wc -l < /out/stage/usr/share/qdistro-build/elf-manifest) dynamic ELFs"
