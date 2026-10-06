#!/usr/bin/env bash
# One-shot build wrapper for the qdistro-vendored Quickshell (see README.md).
#
#   ./build-quickshell.sh                configure + build under $BUILD
#   DESTDIR=/stage ./build-quickshell.sh additionally installs into
#                                        $DESTDIR/$PREFIX (default /usr)
#
# Env:
#   QDSHELL_QS_BUILD_DIR   build dir (default: <here>/src/build)
#   QDSHELL_QS_PREFIX      install prefix (default: /usr — produces
#                          /usr/bin/quickshell + the /usr/bin/qs symlink)
#   QDSHELL_QS_EXTRA_CMAKE extra -D/-U args split on whitespace
#
# Not wired into qdshell's meson.build for the same reason the vendored
# libweston is not wired into qdwin's: it is a different pinned-version
# subproject (cmake, not meson) and should rebuild only when the vendored
# sources change.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/src"
BUILD="${QDSHELL_QS_BUILD_DIR:-$SRC/build}"
PREFIX="${QDSHELL_QS_PREFIX:-/usr}"

[ -d "$SRC" ] || {
    echo "error: $SRC missing — re-extract quickshell @ $(sed -n '1p' "$HERE/VERSION") into src/" >&2
    exit 1
}

# cmake bakes the absolute source path into CMakeCache.txt; a build dir
# configured against a different tree cannot be reused.
if [[ -f "$BUILD/CMakeCache.txt" ]] \
    && ! grep -Fq "CMAKE_HOME_DIRECTORY:INTERNAL=$SRC" "$BUILD/CMakeCache.txt"; then
    echo "note: $BUILD was configured against a different tree — wiping" >&2
    rm -rf "$BUILD"
fi

cmake -S "$SRC" -B "$BUILD" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_PREFIX_PATH="$HERE/cmake" \
    -DDISTRIBUTOR=qdistro \
    ${QDSHELL_QS_EXTRA_CMAKE:-}

cmake --build "$BUILD"

if [ -n "${DESTDIR+x}" ]; then
    DESTDIR="$DESTDIR" cmake --install "$BUILD"
    [ -x "$DESTDIR$PREFIX/bin/quickshell" ] || {
        echo "error: $DESTDIR$PREFIX/bin/quickshell missing after install" >&2
        exit 1
    }
    [ -L "$DESTDIR$PREFIX/bin/qs" ] || {
        echo "error: $DESTDIR$PREFIX/bin/qs symlink missing after install" >&2
        exit 1
    }
fi

echo
echo "Built quickshell:  $BUILD/bin/quickshell"
if [ -n "${DESTDIR+x}" ]; then
    echo "Installed under:   $DESTDIR$PREFIX (bin/quickshell + bin/qs)"
fi
