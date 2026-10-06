#!/usr/bin/env bash
# Runs as root INSIDE a rootless Podman container. /src is a disposable copy.
set -euo pipefail

snap=${QDISTRO_SUBSTRATE_SNAPSHOT:?}
[[ "$snap" =~ ^20[0-9]{6}$ ]] || { echo "invalid snapshot: $snap" >&2; exit 2; }

QDWIN_LIBWESTON_PROFILE=production \
    bash /src/qdwin/libweston-vendored/build-libweston.sh
export PKG_CONFIG_PATH="$(bash /src/qdwin/libweston-vendored/pkgconfig-dir.sh):${PKG_CONFIG_PATH:-}"
DEST=/out/stage/usr/libexec/qdistro/qdwin-libweston \
    bash /src/scripts/install/install-vendored-libweston.sh /src/qdwin

# qdshell's runtime is the vendored upstream Quickshell build — Tumbleweed
# ships only the archived noctalia-qs fork, so the stage carries the binary.
# cmake --install lays out usr/bin/quickshell + the usr/bin/qs symlink.
DESTDIR=/out/stage \
    bash /src/qdshell/quickshell-vendored/build-quickshell.sh

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
for policy in pwd broker session_manager tier1 tier2 presentation; do
    policy_dir=/src/selinux/$policy
    for interface in "$policy_dir"/*.if; do
        [ -f "$interface" ] || continue
        install -m 0644 "$interface" /usr/share/selinux/devel/include/contrib/
    done
    make -C "$policy_dir" "MODULE=qdistro_$policy"
    install -m 0644 "$policy_dir/qdistro_$policy.pp" \
        "/out/stage/usr/share/qdistro-build/selinux/qdistro_$policy.pp"
done

# The runtime-only guest has no policy development tools. Check the broker's
# neverallow negative control here against this snapshot's full policy store.
bash /src/scripts/vm/container-check-broker-ratchet.sh /src/selinux

test -s /out/stage/usr/lib64/weston/qdwin-shell.so
test -s /out/stage/usr/share/qdistro/qml/Qdistro/Qdwin/libqdistro-qdwin.so
test -x /out/stage/usr/bin/quickshell
test -L /out/stage/usr/bin/qs
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
