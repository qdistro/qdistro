#!/usr/bin/env bash
# Headless regression gate: requests on a RELEASED seat's inert wl_seat /
# wl_pointer must not crash the compositor (patch 0005).
#
# qdwin releases a per-stream seat when a view stream's server state goes
# away (qdwin_stream_seat_release) and later frees the struct embedding it.
# A client that raced that release still holds the seat's wl_seat /
# wl_pointer, whose user data libweston has cleared. Upstream
# get_relative_pointer passed that NULL weston_pointer to
# weston_pointer_ensure_pointer_client and SIGSEGVed — qdwin gui/22 S2 in
# ~half of the live runs. get_tablet_seat had the same NULL dereference, and
# tablet-seat resources stayed linked through the released seat's list head.
#
# tests/stale-seat-module.c stands in for qdwin's per-stream seat (created at
# start, released when the client opens a second connection);
# tests/stale-seat-client.py drives, against a headless weston running the
# vendored libweston:
#   control         the release alone, no request: must survive, so a crash
#                   in the cases below is the request's
#   pointer-before  relative pointer for a wl_pointer taken before release,
#                   then explicitly destroyed
#   pointer-after   relative pointer for a wl_pointer taken from the stale seat
#   tablet-before   tablet seat bound before release; the module must report
#                   the seat's tablet list detached after the release
#   tablet-after    tablet seat requested for the stale wl_seat
#   shutdown        pointer-after, then weston is terminated (SIGTERM) while
#                   the inert objects are still alive: must exit cleanly
# Each case needs the client to get roundtrip answers AND weston to survive
# AND weston to have logged the release (so the case really ran on a
# released seat). Unpatched, pointer-* and tablet-after crash weston and
# tablet-before reports the list STILL LINKED.
#
# The library under test is built HERE from the current sources (headless
# profile, incremental, in its own build dir and prefix), so the verdict
# never rests on a prefix some earlier checkout installed.
#
# Env:
#   QDWIN_INERT_RELPTR_PREFIX  test an already-installed prefix instead of
#                           building one (manual use only). Deliberately NOT
#                           QDWIN_LIBWESTON_PREFIX: qci may export that for the
#                           production prefix, which must not opt this gate
#                           out of building the current sources.
#   KEEP_WORK=1             keep the module build and weston logs
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib-major.sh
. "$HERE/lib-major.sh"
CC="${CC:-cc}"

die() { echo "inert-relptr: FAIL: $*" >&2; exit 1; }

command -v weston >/dev/null || die "weston binary not found"
python3 -c 'import pywayland.protocol.relative_pointer_unstable_v1, pywayland.protocol.tablet_unstable_v2' 2>/dev/null \
    || die "python3 pywayland (relative-pointer + tablet-v2 bindings) not importable"

if [ -n "${QDWIN_INERT_RELPTR_PREFIX:-}" ]; then
    PREFIX=$QDWIN_INERT_RELPTR_PREFIX
else
    BUILD="$HERE/src/build-inert-relptr"
    PREFIX="$BUILD/prefix"
    # One build/install tree per checkout: serialize concurrent runs through
    # the build AND the cases (which load from that prefix). The lock lives
    # outside the tree, which a reconfigure removes.
    LOCK="${XDG_RUNTIME_DIR:-/tmp}/qdwin-inert-relptr-$(printf '%s' "$BUILD" | sha256sum | cut -c1-16).lock"
    exec 8>"$LOCK" || die "cannot open lock $LOCK"
    flock 8 || die "cannot take lock $LOCK"
    QDWIN_LIBWESTON_PROFILE=headless QDWIN_LIBWESTON_BUILD_DIR="$BUILD" \
        QDWIN_LIBWESTON_PREFIX="$PREFIX" bash "$HERE/build-libweston.sh" >/dev/null \
        || die "building the headless vendored libweston from $HERE/src failed"
fi

LIBDIR=
for d in "$PREFIX"/lib64 "$PREFIX"/lib/* "$PREFIX"/lib; do
    if [ -f "$d/$LIBWESTON_SONAME.so.0" ] && [ -f "$d/$LIBWESTON_SONAME/headless-backend.so" ]; then
        LIBDIR=$d; break
    fi
done
[ -n "$LIBDIR" ] || die "no $LIBWESTON_SONAME.so.0 + headless backend under '$PREFIX'"

WORK=$(mktemp -d)
trap '[ -n "${KEEP_WORK:-}" ] && echo "inert-relptr: kept $WORK" >&2 || rm -rf "$WORK"' EXIT
MODULE="$WORK/stale-seat-module.so"
# shellcheck disable=SC2046  # pkg-config output is a flag list
"$CC" -shared -fPIC -Wall -Werror -o "$MODULE" "$HERE/tests/stale-seat-module.c" \
    $(PKG_CONFIG_PATH="$LIBDIR/pkgconfig" pkg-config --cflags "$LIBWESTON_SONAME" wayland-server) \
    || die "could not build the test module"

fails=0
run_case() {
    local name="$1" mode="$2" rt log out wp cp rc alive released extra=ok wrc
    rt="$WORK/rt-$name"; log="$WORK/weston-$name.log"; out="$WORK/client-$name.out"
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
        die "$name: weston did not load the test module"
    fi
    grep -qF "$LIBDIR/$LIBWESTON_SONAME.so" "/proc/$wp/maps" \
        || { kill -9 "$wp" 2>/dev/null || true; die "$name: weston did not load the vendored $LIBWESTON_SONAME from $LIBDIR"; }

    if [ "$name" = shutdown ]; then
        XDG_RUNTIME_DIR="$rt" WAYLAND_DISPLAY=wl-relptr \
            timeout 90 python3 "$HERE/tests/stale-seat-client.py" "$mode" --hold >"$out" 2>&1 &
        cp=$!
        rc=1
        for _ in $(seq 1 100); do
            grep -qF "$mode: compositor answered" "$out" && { rc=0; break; }
            kill -0 "$cp" 2>/dev/null || break
            sleep 0.1
        done
        # Graceful termination with the inert objects still alive.
        kill -TERM "$wp" 2>/dev/null
        for _ in $(seq 1 100); do kill -0 "$wp" 2>/dev/null || break; sleep 0.1; done
        if kill -0 "$wp" 2>/dev/null; then
            extra="weston ignored SIGTERM"
        else
            wait "$wp"; wrc=$?
            [ "$wrc" -eq 0 ] || extra="weston exited $wrc on SIGTERM"
        fi
        kill -9 "$cp" 2>/dev/null || true
        wait "$cp" 2>/dev/null || true
        alive=1   # judged by the clean exit above instead
    else
        XDG_RUNTIME_DIR="$rt" WAYLAND_DISPLAY=wl-relptr \
            timeout 30 python3 "$HERE/tests/stale-seat-client.py" "$mode" >"$out" 2>&1
        rc=$?
        sleep 0.3
        if kill -0 "$wp" 2>/dev/null; then alive=1; else alive=0; fi
    fi
    if grep -qF "stale-seat-test: seat released" "$log"; then released=1; else released=0; fi
    if [ "$mode" = tablet-before ]; then
        # >= 1: weston's own helper clients bind tablet seats too.
        grep -qE "tablet seats bound before release: [1-9]" "$log" \
            || extra="no tablet seat was bound before the release"
        grep -qF "tablet seat list detached" "$log" \
            || extra="released seat's tablet list still links client resources"
    fi
    kill -9 "$wp" 2>/dev/null || true
    wait "$wp" 2>/dev/null || true

    if [ "$rc" -eq 0 ] && [ "$alive" -eq 1 ] && [ "$released" -eq 1 ] && [ "$extra" = ok ]; then
        echo "inert-relptr: $name PASS"
    else
        echo "inert-relptr: $name FAIL (client rc=$rc weston_alive=$alive seat_released=$released check=$extra)"
        sed 's/^/  client: /' "$out"
        grep -E 'stale-seat-test|signal|SIGSEGV' "$log" | head -20 | sed 's/^/  weston: /'
        fails=$((fails + 1))
    fi
}

run_case control control
run_case pointer-before pointer-before
run_case pointer-after pointer-after
run_case tablet-before tablet-before
run_case tablet-after tablet-after
run_case shutdown pointer-after
[ "$fails" -eq 0 ] || exit 1
echo "inert-relptr: PASS"
