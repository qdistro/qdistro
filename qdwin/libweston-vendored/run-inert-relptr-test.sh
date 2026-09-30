#!/usr/bin/env bash
# Headless regression gate: zwp_relative_pointer_manager_v1.get_relative_pointer
# on an INERT wl_pointer must not crash the compositor.
#
# qdwin releases a per-stream seat when a view stream's server state goes
# away (qdwin_stream_seat_release). A client that raced that release still
# holds the seat's wl_seat / wl_pointer, whose user data libweston has
# cleared. Upstream get_relative_pointer passed that NULL weston_pointer to
# weston_pointer_ensure_pointer_client and SIGSEGVed — qdwin gui/22 S2 in
# ~half of the live runs. Patch 0005 makes the relative pointer inert too.
#
# tests/stale-seat-module.c stands in for qdwin's per-stream seat (created at
# start, released when a second client connects); tests/stale-seat-client.py drives both inert
# paths against a headless weston running the vendored libweston:
#   pointer-before  wl_pointer taken while the seat was live
#   pointer-after   wl_pointer taken from the stale wl_seat
#   control         the same release, no get_relative_pointer: proves the
#                   module's seat release alone leaves weston alive, so a
#                   crash in the two cases above is the request's
# Each case needs the client to get a roundtrip answer AND weston to survive
# AND weston to have logged the release (so the case really ran on a
# released seat). On the unpatched library both pointer cases crash weston.
#
# Env:
#   QDWIN_LIBWESTON_PREFIX  vendored prefix to test
#                           (default /tmp/qdwin-libweston-prod-prefix)
#   KEEP_WORK=1             keep the module build and weston logs
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib-major.sh
. "$HERE/lib-major.sh"
PREFIX="${QDWIN_LIBWESTON_PREFIX:-/tmp/qdwin-libweston-prod-prefix}"
CC="${CC:-cc}"

die() { echo "inert-relptr: FAIL: $*" >&2; exit 1; }

command -v weston >/dev/null || die "weston binary not found"
python3 -c 'import pywayland.protocol.relative_pointer_unstable_v1' 2>/dev/null \
    || die "python3 pywayland (with relative-pointer bindings) not importable"

LIBDIR=
for d in "$PREFIX"/lib64 "$PREFIX"/lib/* "$PREFIX"/lib; do
    if [ -f "$d/$LIBWESTON_SONAME.so.0" ] && [ -f "$d/$LIBWESTON_SONAME/headless-backend.so" ]; then
        LIBDIR=$d; break
    fi
done
[ -n "$LIBDIR" ] || die "no $LIBWESTON_SONAME.so.0 + headless backend under '$PREFIX'; build it with build-libweston.sh"

WORK=$(mktemp -d)
trap '[ -n "${KEEP_WORK:-}" ] && echo "inert-relptr: kept $WORK" >&2 || rm -rf "$WORK"' EXIT
MODULE="$WORK/stale-seat-module.so"
# shellcheck disable=SC2046  # pkg-config output is a flag list
"$CC" -shared -fPIC -Wall -Werror -o "$MODULE" "$HERE/tests/stale-seat-module.c" \
    $(PKG_CONFIG_PATH="$LIBDIR/pkgconfig" pkg-config --cflags "$LIBWESTON_SONAME" wayland-server) \
    || die "could not build the test module"

fails=0
run_case() {
    local mode="$1" rt log wp rc alive released
    rt="$WORK/rt-$mode"; log="$WORK/weston-$mode.log"
    mkdir -m 700 "$rt"
    XDG_RUNTIME_DIR="$rt" LD_LIBRARY_PATH="$LIBDIR" \
      WESTON_MODULE_MAP="headless-backend.so=$LIBDIR/$LIBWESTON_SONAME/headless-backend.so" \
      weston --backend=headless --renderer=pixman --width=320 --height=240 \
        --socket=wl-relptr --modules="$MODULE" >"$log" 2>&1 &
    wp=$!
    for _ in $(seq 1 100); do
        grep -qF "stale-seat-test: seat ready" "$log" && [ -S "$rt/wl-relptr" ] && break
        kill -0 "$wp" 2>/dev/null || break
        sleep 0.1
    done
    if ! grep -qF "stale-seat-test: seat ready" "$log"; then
        kill -9 "$wp" 2>/dev/null || true
        sed 's/^/  weston: /' "$log" >&2
        die "$mode: weston did not load the test module"
    fi
    grep -qF "$LIBDIR/$LIBWESTON_SONAME.so" "/proc/$wp/maps" \
        || { kill -9 "$wp" 2>/dev/null || true; die "$mode: weston did not load the vendored $LIBWESTON_SONAME from $LIBDIR"; }

    XDG_RUNTIME_DIR="$rt" WAYLAND_DISPLAY=wl-relptr \
        timeout 30 python3 "$HERE/tests/stale-seat-client.py" "$mode"
    rc=$?
    sleep 0.3
    if kill -0 "$wp" 2>/dev/null; then alive=1; else alive=0; fi
    if grep -qF "stale-seat-test: seat released" "$log"; then released=1; else released=0; fi
    kill -9 "$wp" 2>/dev/null || true
    wait "$wp" 2>/dev/null || true

    if [ "$rc" -eq 0 ] && [ "$alive" -eq 1 ] && [ "$released" -eq 1 ]; then
        echo "inert-relptr: $mode PASS"
    else
        echo "inert-relptr: $mode FAIL (client rc=$rc weston_alive=$alive seat_released=$released)"
        grep -E 'signal|backtrace|^\[.*\] #|SIGSEGV|relative_pointer' "$log" | head -20 | sed 's/^/  weston: /'
        fails=$((fails + 1))
    fi
}

run_case control
run_case pointer-before
run_case pointer-after
[ "$fails" -eq 0 ] || exit 1
echo "inert-relptr: PASS"
