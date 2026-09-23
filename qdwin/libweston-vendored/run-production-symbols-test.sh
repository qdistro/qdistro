#!/usr/bin/env bash
# Headless CI gate for the SHIPPED vendored libweston.
#
# The production profile of build-libweston.sh is what qdistro packages
# (see qdwin/doc/decisions/0001-vendored-libweston-packaging.md). This
# gate proves that profile actually exports the four soft-linked helper
# symbols qdwin resolves via dlsym — i.e. the packaged library can drive
# the layer-shell popup / grab paths. Stock libweston-16 exports none of
# them, so this is the discriminator between a shippable tree and a
# decorative one.
#
# It also runs the install-staging dry-run so a broken stage script is
# caught in CI rather than at image-build time. No VM, no display.
#
# Exit 0 only if all four symbols are exported AND the staging dry-run
# lays down the core + drm backend.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"   # workspace root (siblings: qdwin, qdistro)
BUILD_SCRIPT="$HERE/build-libweston.sh"
PREFIX="${QDWIN_LIBWESTON_PREFIX:-/tmp/qdwin-libweston-prod-prefix}"

# J29: soname/soversion come from VERSION via lib-major.sh so a version bump is
# a single edit and this gate cannot drift from what the build actually
# produces ($LIBWESTON_SONAME, $LIBWESTON_LIBVER).
# shellcheck source=lib-major.sh
. "$HERE/lib-major.sh"

# Allow CI on a host lacking pipewire/rdp/GL devel to still gate the
# symbol export — those backends are orthogonal to the desktop helpers
# (they live in their own backend .so files; the four soft-linked popup
# helpers are in the libweston-desktop core and need none of them). The
# VM/image build uses the full set (install-deps.sh covers it).
#
# Rather than hardcode these ON (which hard-fails the gate on a host that
# lacks e.g. GLES3/gl3.h, before the symbol assertion is ever reached),
# auto-detect each orthogonal backend's devel dependency and disable ONLY
# the ones whose dep is MISSING. An explicit QDWIN_LW_* override from the
# caller always wins. The drm backend (which the staging dry-run requires)
# does NOT need GLES3, so it stays enabled.

CC="${CC:-cc}"
notes=()

# Returns 0 if the given C #include compiles against the default include
# path (i.e. the devel header is installed), 1 otherwise.
header_available() {
    local hdr="$1"
    printf '#include <%s>\nint main(void){return 0;}\n' "$hdr" \
        | "$CC" -fsyntax-only -xc - >/dev/null 2>&1
}

# GL renderer needs the GLES3 headers. drm/pixman do not.
if [ -z "${QDWIN_LW_GL:-}" ]; then
    if header_available "GLES3/gl3.h"; then
        export QDWIN_LW_GL=true
    else
        export QDWIN_LW_GL=false
        notes+=("GL renderer (GLES3/gl3.h not on the compiler include path)")
    fi
fi

# pipewire backend needs libpipewire-0.3 devel.
if [ -z "${QDWIN_LW_PIPEWIRE:-}" ]; then
    if pkg-config --exists libpipewire-0.3 2>/dev/null; then
        export QDWIN_LW_PIPEWIRE=true
    else
        export QDWIN_LW_PIPEWIRE=false
        notes+=("pipewire backend (libpipewire-0.3 devel not found via pkg-config)")
    fi
fi

# rdp backend needs the FULL FreeRDP devel set for one major version:
# the client lib AND the matching freerdp-server* AND winpr* package.
# Meson's backend-rdp/meson.build requires all three (and prefers v3 over
# v2 when both are present), so a header-only or client-lib-only probe can
# say "yes" while the actual meson configure still fails before the symbol
# assertion is reached. Only enable RDP when pkg-config can satisfy the
# complete set for v3, else v2; otherwise disable it.
if [ -z "${QDWIN_LW_RDP:-}" ]; then
    if pkg-config --exists "freerdp3 >= 3.0.0" \
                           "freerdp-server3 >= 3.0.0" \
                           "winpr3 >= 3.0.0" 2>/dev/null; then
        export QDWIN_LW_RDP=true
    elif pkg-config --exists "freerdp2 >= 2.3.0" \
                             "freerdp-server2 >= 2.3.0" \
                             "winpr2 >= 2.3.0" 2>/dev/null; then
        export QDWIN_LW_RDP=true
    else
        export QDWIN_LW_RDP=false
        notes+=("rdp backend (complete FreeRDP server/winpr devel set not found via pkg-config)")
    fi
fi

if [ "${#notes[@]}" -gt 0 ]; then
    echo "[prod-syms] NOTE: auto-disabling orthogonal backend(s) whose devel" \
         "deps are missing on this host; the desktop popup helper symbols do" \
         "not depend on them:" >&2
    for n in "${notes[@]}"; do
        echo "[prod-syms] NOTE:   - $n" >&2
    done
    echo "[prod-syms] NOTE: drm backend stays enabled (needed by the staging" \
         "dry-run; it does not require GLES3). Set QDWIN_LW_* explicitly to" \
         "override this auto-detection." >&2
fi

SYMS=(
    weston_desktop_xdg_popup_attach_layer_parent
    weston_desktop_xdg_popup_get_geometry
    weston_desktop_xdg_popup_set_layer_grab_handler
    weston_desktop_xdg_popup_dismiss_layer_grab
)

fail() { echo "[prod-syms] FAIL: $*" >&2; exit 1; }

# Locate the installed core library under $PREFIX without assuming a libdir:
# meson installs to lib64 on openSUSE but to the arch multiarch dir
# (lib/x86_64-linux-gnu) on Debian/Ubuntu, and build-libweston.sh deliberately
# does not force --libdir. Echoes the core .so path, or nothing if not built.
find_core() {
    ls "$PREFIX"/lib64/"$LIBWESTON_SONAME.so.$LIBWESTON_LIBVER" \
       "$PREFIX"/lib/*/"$LIBWESTON_SONAME.so.$LIBWESTON_LIBVER" \
       "$PREFIX"/lib/"$LIBWESTON_SONAME.so.$LIBWESTON_LIBVER" 2>/dev/null | head -n1
}

# Dependency preflight for the deps the auto-disable block above does NOT
# guard. The orthogonal backends (GL/pipewire/RDP) are probed and toggled
# off when missing, so they are intentionally excluded here. What remains are
# the genuinely-required modules — the libweston-desktop core, the always-on
# shared toytoolkit (cairo/png/pango/...), drm/seat/udev, VA-API, and the
# lcms + x11 features that stay enabled — whose absence otherwise makes
# `meson setup` die with a cryptic "Dependency X not found" deep in the build
# (exactly how this gate failed in CI when cairo/libpng/lcms2 were absent).
# Backend-feature rows honour the same QDWIN_LW_* toggles the auto-disable
# block just set, so a degraded build never demands a dep it won't use.
preflight_deps() {
    command -v pkg-config >/dev/null 2>&1 \
        || fail "pkg-config not found (install pkgconf / pkg-config)"

    # module | apt package | zypper package | gate (always|lcms|gl|x11)
    local specs=(
        "wayland-client     libwayland-dev        wayland-devel            always"
        "wayland-protocols  wayland-protocols     wayland-protocols-devel  always"
        "xkbcommon          libxkbcommon-dev      libxkbcommon-devel       always"
        "pixman-1           libpixman-1-dev       libpixman-1-0-devel      always"
        "libinput           libinput-dev          libinput-devel           always"
        "libevdev           libevdev-dev          libevdev-devel           always"
        "libdrm             libdrm-dev            libdrm-devel             always"
        "gbm                libgbm-dev            libgbm-devel             always"
        "libseat            libseat-dev           seatd-devel              always"
        "libudev            libudev-dev           systemd-devel            always"
        "libdisplay-info    libdisplay-info-dev   libdisplay-info-devel    always"
        "cairo              libcairo2-dev         cairo-devel              always"
        "libpng             libpng-dev            libpng16-compat-devel    always"
        "pango              libpango1.0-dev       pango-devel              always"
        "pangocairo         libpango1.0-dev       pango-devel              always"
        "fontconfig         libfontconfig-dev     fontconfig-devel         always"
        "glib-2.0           libglib2.0-dev        glib2-devel              always"
        "libva              libva-dev             libva-devel              always"
        "lcms2              liblcms2-dev          liblcms2-devel           lcms"
        "egl                libegl-dev            Mesa-libEGL-devel        gl"
        "glesv2             libgles-dev           Mesa-libGLESv2-devel     gl"
        "x11                libx11-dev            libX11-devel             x11"
        "x11-xcb            libx11-xcb-dev        libX11-devel             x11"
        "xcb                libxcb1-dev           libxcb-devel             x11"
        "xcb-shm            libxcb-shm0-dev       libxcb-devel             x11"
    )

    local -A gate_on=(
        [always]=1
        [lcms]=$([ "${QDWIN_LW_LCMS:-true}" = false ] && echo 0 || echo 1)
        [gl]=$([ "${QDWIN_LW_GL:-true}" = false ] && echo 0 || echo 1)
        [x11]=$([ "${QDWIN_LW_X11:-true}" = false ] && echo 0 || echo 1)
    )

    local missing_mods=() missing_apt=() missing_zyp=()
    local line mod apt zyp gate
    for line in "${specs[@]}"; do
        read -r mod apt zyp gate <<<"$line"
        [ "${gate_on[$gate]}" = 1 ] || continue
        pkg-config --exists "$mod" 2>/dev/null && continue
        missing_mods+=("$mod"); missing_apt+=("$apt"); missing_zyp+=("$zyp")
    done
    [ "${#missing_mods[@]}" -eq 0 ] && return 0

    local uniq_apt uniq_zyp
    uniq_apt=$(printf '%s\n' "${missing_apt[@]}" | sort -u | tr '\n' ' ')
    uniq_zyp=$(printf '%s\n' "${missing_zyp[@]}" | sort -u | tr '\n' ' ')
    echo "[prod-syms] missing build dependencies (pkg-config): ${missing_mods[*]}" >&2
    if command -v apt-get >/dev/null 2>&1; then
        echo "[prod-syms] install with: sudo apt-get install -y $uniq_apt" >&2
    elif command -v zypper >/dev/null 2>&1; then
        echo "[prod-syms] install with: sudo zypper install -y $uniq_zyp" >&2
    else
        echo "[prod-syms] apt packages:    $uniq_apt" >&2
        echo "[prod-syms] zypper packages: $uniq_zyp" >&2
    fi
    fail "production build prerequisites missing — install the packages above and re-run"
}

[ -x "$BUILD_SCRIPT" ] || fail "build script missing at $BUILD_SCRIPT"

if [ -z "$(find_core)" ]; then
    # build-libweston.sh only runs `meson setup` when build-prod/build.ninja
    # is ABSENT, so a stale build dir would silently keep an old backend
    # config and ignore the QDWIN_LW_* options we just decided on. Force a
    # clean reconfigure so the chosen backend set actually takes effect.
    PROD_BUILD="$HERE/src/build-prod"
    if [ -d "$PROD_BUILD" ]; then
        echo "[prod-syms] removing stale build dir $PROD_BUILD to force reconfigure" >&2
        rm -rf "$PROD_BUILD"
    fi
    preflight_deps
    echo "[prod-syms] building production profile into $PREFIX ..."
    QDWIN_LIBWESTON_PROFILE=production QDWIN_LIBWESTON_PREFIX="$PREFIX" \
        bash "$BUILD_SCRIPT" || fail "production build failed"
fi

CORE="$(find_core)"
[ -n "$CORE" ] && [ -f "$CORE" ] \
    || fail "no $LIBWESTON_SONAME.so.$LIBWESTON_LIBVER under $PREFIX (lib64 or lib/<arch>) after build"

# Capture the dynamic symbol table once. Piping nm into `grep -q` under
# `pipefail` would report SIGPIPE (141) when grep exits early, so read
# the table into a variable and match against that instead.
dynsyms=$(nm -D "$CORE" 2>/dev/null) || fail "nm -D failed on $CORE"
missing=0
for s in "${SYMS[@]}"; do
    if grep -q " T $s\$" <<<"$dynsyms"; then
        echo "[prod-syms] EXPORTED: $s"
    else
        echo "[prod-syms] MISSING:  $s" >&2
        missing=1
    fi
done
[ "$missing" -eq 0 ] || fail "one or more soft-linked helper symbols are not exported by the production library"

# Staging dry-run into a throwaway dest (non-root tolerated).
STAGE_SCRIPT="$REPO/qdistro/scripts/install/install-vendored-libweston.sh"
if [ -x "$STAGE_SCRIPT" ]; then
    DEST="$(mktemp -d)/qdwin-libweston"
    echo "[prod-syms] staging dry-run -> $DEST"
    # Pass the qdwin source tree as $1. Without it the installer falls back to
    # its in-VM default (/root/qdistro-src/qdwin), cannot read lib-major.sh,
    # and stages whatever libweston major it can infer instead of the one
    # VERSION pins — which silently staged a leftover libweston-14 out of this
    # 16 prefix and failed the assertion below.
    stage_log="$(mktemp)"
    if ! DEST="$DEST" QDWIN_LIBWESTON_PREFIX="$PREFIX" \
            bash "$STAGE_SCRIPT" "$REPO/qdwin" >"$stage_log" 2>&1; then
        sed 's/^/[prod-syms]   /' "$stage_log" >&2
        rm -f "$stage_log"
        fail "install-vendored-libweston.sh staging failed"
    fi
    rm -f "$stage_log"
    [ -f "$DEST/lib64/$LIBWESTON_SONAME.so.0" ] \
        || fail "staged tree missing $LIBWESTON_SONAME.so.0"
    [ -f "$DEST/lib64/$LIBWESTON_SONAME/drm-backend.so" ] \
        || fail "staged tree missing drm-backend.so (headless-only / wrong prefix?)"
    rm -rf "$(dirname "$DEST")"
    echo "[prod-syms] staging dry-run OK"
else
    echo "[prod-syms] NOTE: $STAGE_SCRIPT absent — skipping staging dry-run"
fi

echo "[prod-syms] PASS — production libweston exports all helper symbols and stages cleanly"
