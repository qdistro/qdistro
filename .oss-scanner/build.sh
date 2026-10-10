#!/usr/bin/env bash
# Run as root in the prepared scanner image. No downloads or VM orchestration.
set -euo pipefail
cd "$(dirname "$0")/.."
jobs=${OSS_SCANNER_JOBS:-2}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { echo 'invalid OSS_SCANNER_JOBS' >&2; exit 2; }
export CFLAGS="${CFLAGS:--O1 -g -fno-omit-frame-pointer}"
export CXXFLAGS="${CXXFLAGS:--O1 -g -fno-omit-frame-pointer}"
export QDWIN_LIBWESTON_PROFILE=production
bash qdwin/libweston-vendored/build-libweston.sh
lwpc=$(bash qdwin/libweston-vendored/pkgconfig-dir.sh) || exit
export PKG_CONFIG_PATH="$lwpc:${PKG_CONFIG_PATH:-}"
# Disable PCH even when /proc/meminfo shows the large host's RAM rather than
# the scanner's cgroup limit. Retain debug info in the vendored runtime.
QDSHELL_QS_EXTRA_CMAKE='-DNO_PCH=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo' \
    DESTDIR=/ bash qdshell/quickshell-vendored/build-quickshell.sh
for component in qdwin daemons qdshell; do
    build="$component/build-oss"
    args=(--prefix=/usr --buildtype=debugoptimized -Db_ndebug=false)
    if [ -f "$build/build.ninja" ]; then
        meson setup --reconfigure "$build" "$component" "${args[@]}"
    else
        meson setup "$build" "$component" "${args[@]}"
    fi
    meson compile -C "$build" -j "$jobs"
    meson install -C "$build"
done
mkdir -p qsu/build-oss
read -r -a cflags <<< "$CFLAGS"
cc "${cflags[@]}" -Wall -Wextra -Wformat=2 -Werror=format-security \
    -o qsu/build-oss/qsu qsu/qsu.c
SIP_SRC="$PWD/qdterm/qtermwidget-pyqt" bash qdterm/util/build-sip.sh
for policy in pwd broker session_manager tier1 tier2 presentation; do
    policy_dir="selinux/$policy"
    for interface in "$policy_dir"/*.if; do
        [ ! -f "$interface" ] || install -m 0644 "$interface" /usr/share/selinux/devel/include/contrib/
    done
    make -C "$policy_dir" "MODULE=qdistro_$policy"
done
bash scripts/vm/container-check-broker-ratchet.sh "$PWD/selinux"
