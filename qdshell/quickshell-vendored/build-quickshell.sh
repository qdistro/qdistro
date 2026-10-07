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
#   QDSHELL_QS_BUILD_JOBS  ninja parallelism (default: nproc, capped by RAM)
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

# A Qt6 cc1plus step — above all the per-module PCH compiles — needs well
# over a GiB; in the 4 GiB test VMs full nproc parallelism gets cc1plus
# OOM-killed. Cap jobs to ~1.5 GiB each, and under ~6 GiB also drop the PCH
# sets (each pchset gch recompiles Qt6 headers once per module).
JOBS="${QDSHELL_QS_BUILD_JOBS:-$(nproc)}"
MEM_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
MEM_JOBS=$(( MEM_KB / 1500000 ))
[ "$MEM_JOBS" -ge 1 ] || MEM_JOBS=1
[ "$MEM_JOBS" -lt "$JOBS" ] && JOBS=$MEM_JOBS
PCH=()
if [ "$MEM_KB" -lt 6291456 ]; then
    PCH=(-DNO_PCH=ON)
fi

cmake -S "$SRC" -B "$BUILD" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_PREFIX_PATH="$HERE/cmake" \
    -DDISTRIBUTOR=qdistro \
    ${PCH[@]+"${PCH[@]}"} \
    ${QDSHELL_QS_EXTRA_CMAKE:-}

cmake --build "$BUILD" --parallel "$JOBS"

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
