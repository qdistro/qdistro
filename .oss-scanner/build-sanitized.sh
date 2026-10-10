#!/usr/bin/env bash
# Optional offline ASan/UBSan build; never installs over the ordinary binaries.
set -euo pipefail
cd "$(dirname "$0")/.."
component=${1:-qdwin}
case "$component" in qdwin|daemons) ;; *) echo 'usage: build-sanitized.sh [qdwin|daemons]' >&2; exit 2;; esac
lwpc=$(bash qdwin/libweston-vendored/pkgconfig-dir.sh) || exit
export PKG_CONFIG_PATH="$lwpc:${PKG_CONFIG_PATH:-}"
build="$component/build-oss-sanitized"
args=(--prefix=/usr --buildtype=debug '-Db_sanitize=address,undefined' -Db_lundef=false)
if [ -f "$build/build.ninja" ]; then
    meson setup --reconfigure "$build" "$component" "${args[@]}"
else
    meson setup "$build" "$component" "${args[@]}"
fi
meson compile -C "$build" -j "${OSS_SCANNER_JOBS:-2}"
