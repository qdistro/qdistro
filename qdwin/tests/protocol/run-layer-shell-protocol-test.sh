#!/bin/bash
# Headless protocol-suite harness for test_zwlr_layer_shell.py.
#
# Boots a private headless weston with the built qdwin-shell.so, generates
# the pywayland bindings the test client imports, runs the client against
# that compositor, and propagates its exit code. Driven by
# `meson test --suite protocol` (see meson.build), but also runnable by
# hand for debugging.
#
# Unlike qdwin/run-protocol-tests.sh (which assumes a VM: a fixed
# wayland-77 socket under /run/user/1000 and runuser -u admin), this runs
# entirely as the invoking user in a throwaway XDG_RUNTIME_DIR, so it works
# on a stock CI host with no seat and no root.
#
# Every prerequisite is optional: if weston, the headless backend,
# pywayland, or its scanner is missing, the harness exits 77 (meson's SKIP
# code) instead of failing — the suite is a gate that only fires where the
# tools exist.
#
# Positional args (supplied by meson; see the test() in meson.build):
#   $1  path to the built qdwin-shell.so
#   $2  path to test_zwlr_layer_shell.py
#   $3  path to wlr-layer-shell-unstable-v1.xml
#   $4  path to qdwin-shell-v1.xml
#
# Env knobs:
#   QDWIN_USE_VENDORED_LIBWESTON=1
#       LD_LIBRARY_PATH-prefix the qdistro-vendored libweston-16 (NULL-parent
#       xdg_popup / layer-popup-grab patch) so the two grab/reposition
#       discriminator tests run instead of SKIP. Requires the vendored .so to
#       be installed (see QDWIN_VENDORED_LIBWESTON_PREFIX).
#   QDWIN_VENDORED_LIBWESTON_PREFIX  (default /usr/libexec/qdistro/qdwin-libweston)
#       prefix whose lib64/ holds the vendored libweston-16.so.
#
# Phase 1.6 / fable-testing p2-qdwin-compiled-tests.md item 2.

set -u

SKIP=77   # meson "test skipped" exit status (exitcode protocol)

skip() { echo "protocol-suite SKIP: $*" >&2; exit "$SKIP"; }
die()  { echo "protocol-suite FAIL: $*" >&2; exit 1; }

PLUGIN="${1:-}"
CLIENT="${2:-}"
LAYER_SHELL_XML="${3:-}"
QDWIN_SHELL_XML="${4:-}"

[ -n "$PLUGIN" ] && [ -n "$CLIENT" ] && [ -n "$LAYER_SHELL_XML" ] \
    && [ -n "$QDWIN_SHELL_XML" ] || die "usage: $0 <plugin.so> <client.py> <layer-shell.xml> <qdwin-shell.xml>"

# meson runs the test with CWD = build dir and substitutes the build target
# (qdwin-shell.so) as a path RELATIVE to it. A relative shell= in weston.ini
# is read as a module NAME and resolved from weston's install dir — silently
# loading the stale /usr/lib64/weston copy instead of the just-built one. Pin
# every path to absolute before we chdir away.
abspath() { case "$1" in /*) printf '%s\n' "$1";; *) printf '%s/%s\n' "$(pwd)" "$1";; esac; }
PLUGIN="$(abspath "$PLUGIN")"
CLIENT="$(abspath "$CLIENT")"
LAYER_SHELL_XML="$(abspath "$LAYER_SHELL_XML")"
QDWIN_SHELL_XML="$(abspath "$QDWIN_SHELL_XML")"

# J29: the libweston major this plugin targets comes from qdwin's single source
# of truth (libweston-vendored/VERSION) so the probes below track a version bump
# with no edits.
_HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../../libweston-vendored/lib-major.sh
. "$_HERE/../../libweston-vendored/lib-major.sh"

# --- prerequisite probes: missing tooling => SKIP, not FAIL ---------------
command -v weston >/dev/null 2>&1 || skip "weston not in PATH"
command -v pkg-config >/dev/null 2>&1 || skip "pkg-config not available"

# The weston FRONTEND must link the same libweston major the plugin was built
# against. weston resolves --backend=... and shell= against its own libweston,
# so a major mismatch is a silent ABI mismatch, not a load error: the struct
# layouts differ, weston SIGABRTs mid-startup and the client dies with SIGSEGV
# — a crash that looks like a product failure but only says "wrong weston".
# openSUSE ships weston 14 while the vendored tree is pinned to 16, and the
# vendored build deliberately omits the weston frontend (libweston only), so
# there is no matching frontend to point at on such a host. SKIP with the
# reason; the matching-major coverage is the VM lane, whose golden image ships
# weston $LIBWESTON_MAJOR.
#
# Probe with ldd, not `objdump -p ... NEEDED`: the weston binary's direct
# NEEDED list is only libexec_weston.so.0 (+libc) — libweston comes in one
# level down, through that frontend library — so a NEEDED-only scan finds
# nothing and the check silently never fires. ldd resolves transitively.
#
# An undeterminable major SKIPs too, rather than proceeding: running on an
# unverified ABI combination is what produced the SIGABRT/SIGSEGV pair this
# probe exists to explain, and a crash attributed to the product is worse than
# an honest skip.
command -v ldd >/dev/null 2>&1 || skip "ldd not available (cannot verify the weston/libweston ABI major)"
_weston_major=$(ldd "$(command -v weston)" 2>/dev/null \
    | sed -n 's/.*[^a-z]libweston-\([0-9][0-9]*\)\.so\..*/\1/p' | head -n1)
[ -n "${_weston_major:-}" ] \
    || skip "cannot resolve a libweston major from the weston frontend (ldd found none); refusing to run on an unverified ABI combination"
[ "$_weston_major" = "$LIBWESTON_MAJOR" ] \
    || skip "weston frontend links libweston-$_weston_major but the plugin targets libweston-$LIBWESTON_MAJOR (VERSION); ABI mismatch — run this suite in the VM lane"
python3 -c 'import pywayland, pywayland.scanner' 2>/dev/null \
    || skip "pywayland (+scanner) not importable"

# The compositor-under-test must have been built. If meson built it as a
# dependency (it does — see test() 'depends:') this always exists; a missing
# artifact is a real build error, not an environment skip.
[ -f "$PLUGIN" ] || die "built qdwin-shell.so not found at $PLUGIN"
[ -f "$CLIENT" ] || die "test client not found at $CLIENT"

# Headless backend: weston resolves --backend=headless-backend.so from its
# module dir, which libweston keeps at <libdir>/$LIBWESTON_SONAME/.
LIBWESTON_LIBDIR="$(pkg-config --variable=libdir "$LIBWESTON_SONAME" 2>/dev/null || true)"
HEADLESS_SO=""
for d in "$LIBWESTON_LIBDIR/$LIBWESTON_SONAME" "/usr/lib64/$LIBWESTON_SONAME" \
         "/usr/lib/$LIBWESTON_SONAME" "/usr/lib/x86_64-linux-gnu/$LIBWESTON_SONAME"; do
    if [ -f "$d/headless-backend.so" ]; then HEADLESS_SO="$d/headless-backend.so"; break; fi
done
[ -n "$HEADLESS_SO" ] || skip "headless-backend.so not found (no headless weston)"

# Protocol XMLs the bindings need: wayland.xml + xdg-shell.xml from the
# system data dirs, plus the two repo-vendored XMLs passed as args.
WL_DATADIR="$(pkg-config --variable=pkgdatadir wayland-scanner 2>/dev/null || true)"
WAYLAND_XML=""
for f in "$WL_DATADIR/wayland.xml" /usr/share/wayland/wayland.xml; do
    [ -f "$f" ] && { WAYLAND_XML="$f"; break; }
done
[ -n "$WAYLAND_XML" ] || skip "wayland.xml not found (wayland-scanner data missing)"

WP_DATADIR="$(pkg-config --variable=pkgdatadir wayland-protocols 2>/dev/null || true)"
XDG_SHELL_XML=""
for f in "$WP_DATADIR/stable/xdg-shell/xdg-shell.xml" \
         /usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml; do
    [ -f "$f" ] && { XDG_SHELL_XML="$f"; break; }
done
[ -n "$XDG_SHELL_XML" ] || skip "xdg-shell.xml not found (wayland-protocols missing)"

# --- optional vendored libweston-16 -------------------------------------
QDWIN_USE_VENDORED_LIBWESTON="${QDWIN_USE_VENDORED_LIBWESTON:-0}"
QDWIN_VENDORED_LIBWESTON_PREFIX="${QDWIN_VENDORED_LIBWESTON_PREFIX:-/usr/libexec/qdistro/qdwin-libweston}"
VENDORED_LD=""
if [ "$QDWIN_USE_VENDORED_LIBWESTON" = "1" ]; then
    vso="$QDWIN_VENDORED_LIBWESTON_PREFIX/lib64/$LIBWESTON_SONAME.so.$LIBWESTON_LIBVER"
    [ -f "$vso" ] || die "QDWIN_USE_VENDORED_LIBWESTON=1 but no vendored .so at $vso"
    VENDORED_LD="$QDWIN_VENDORED_LIBWESTON_PREFIX/lib64"
    echo "protocol-suite: using vendored libweston at $QDWIN_VENDORED_LIBWESTON_PREFIX" >&2
fi

# --- staging: bindings at <stage>/qdshell/protocol, client one dir over ---
# The client does sys.path.insert(0, HERE/../qdshell), so the generated
# 'protocol' package must live at <stage>/qdshell/protocol with the client
# at <stage>/qdwin-test/ (mirrors the fresh-VM layout the client assumes).
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/qdwin-proto-suite.XXXXXX")" || die "mktemp failed"
WPID=""
cleanup() {
    [ -n "$WPID" ] && kill "$WPID" 2>/dev/null
    [ -n "$WPID" ] && wait "$WPID" 2>/dev/null
    rm -rf "$STAGE" 2>/dev/null
}
# Signal handlers must re-exit: a bare `trap cleanup TERM` (what meson sends on
# timeout) would run cleanup and then RESUME the script with $STAGE already
# gone. The explicit exit re-fires the EXIT trap exactly once (idempotent).
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

mkdir -p "$STAGE/qdshell" "$STAGE/qdwin-test"
cp "$CLIENT" "$STAGE/qdwin-test/" || die "cannot stage client"

python3 -m pywayland.scanner \
    -i "$WAYLAND_XML" "$LAYER_SHELL_XML" "$XDG_SHELL_XML" "$QDWIN_SHELL_XML" \
    -o "$STAGE/qdshell/protocol" >/dev/null 2>"$STAGE/scanner.err" \
    || { echo "--- scanner stderr ---" >&2; cat "$STAGE/scanner.err" >&2; \
         die "pywayland scanner failed to generate bindings"; }

# --- boot a private headless weston -------------------------------------
RUNDIR="$STAGE/run"
mkdir -p "$RUNDIR"
chmod 700 "$RUNDIR"
WLD="wayland-proto-$$"
WLOG="$STAGE/weston.log"

cat > "$STAGE/weston.ini" <<EOF
[core]
shell=$PLUGIN
require-input=false
idle-time=0
EOF

# allowed_uid defaults to the weston process uid (== us), so qdwin_shell_v1
# is advertised to our client and the v19 register_hotkey test can run.
# Build LD_LIBRARY_PATH WITHOUT a trailing colon: an empty component makes
# the dynamic loader also search the cwd, a footgun for the compositor proc.
if [ -n "$VENDORED_LD" ]; then
    WESTON_LD="$VENDORED_LD${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
else
    WESTON_LD="${LD_LIBRARY_PATH:-}"
fi
XDG_RUNTIME_DIR="$RUNDIR" \
LD_LIBRARY_PATH="$WESTON_LD" \
    weston --backend=headless-backend.so --renderer=pixman \
           --config="$STAGE/weston.ini" --socket="$WLD" \
           >"$WLOG" 2>&1 &
WPID=$!

# Wait for the socket (or early weston death).
ready=""
for _ in $(seq 1 20); do
    if [ -S "$RUNDIR/$WLD" ]; then ready=1; break; fi
    if ! kill -0 "$WPID" 2>/dev/null; then break; fi
    sleep 0.5
done
if [ -z "$ready" ]; then
    echo "--- weston log ---" >&2; tail -40 "$WLOG" >&2
    # A backend/renderer the running weston can't load is an ENVIRONMENT
    # gap (e.g. weston/libweston ABI skew on the host) → SKIP, not FAIL.
    # A failure to load OUR plugin (qdwin-shell.so) is a real defect → FAIL.
    if grep -qiE 'Failed to load module.*(headless-backend|pixman|renderer)|fatal: .*backend|Could not load backend' "$WLOG" \
       && ! grep -qi 'Failed to load module.*qdwin-shell' "$WLOG"; then
        skip "weston could not load the headless backend / pixman renderer on this host"
    fi
    die "headless weston socket never appeared"
fi

# --- run the client against the compositor ------------------------------
# QDWIN_COMPOSITOR_LOG: grab-handler test reads it. QDWIN_WESTON_LOG: v19
# hotkey test reads it. Both point at the same weston log.
cd "$STAGE/qdwin-test" || die "cannot cd to staged client dir"
XDG_RUNTIME_DIR="$RUNDIR" \
QDWIN_COMPOSITOR_LOG="$WLOG" \
QDWIN_WESTON_LOG="$WLOG" \
QDWIN_USE_VENDORED_LIBWESTON="$QDWIN_USE_VENDORED_LIBWESTON" \
    python3 test_zwlr_layer_shell.py "$WLD"
RC=$?

echo "" >&2
echo "=== weston log (zwlr / qdwin layer lines) ===" >&2
grep -nE 'zwlr_layer|qdwin: layer|qdwin: register_hotkey|qdwin: unregister_hotkey' \
    "$WLOG" >&2 || echo "(none)" >&2

exit "$RC"
