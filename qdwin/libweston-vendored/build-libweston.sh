#!/usr/bin/env bash
# One-shot build wrapper for the qdistro-vendored libweston-16.
#
# Two profiles (set QDWIN_LIBWESTON_PROFILE):
#
#   headless (default) — minimal headless-only build for host-side
#       protocol tests (run-null-parent-test.sh, run-protocol-tests.sh).
#       Output under /tmp/qdwin-libweston-prefix. Nothing global touched.
#
#   production         — full backend set (drm + pipewire + rdp + wayland
#       + x11 + headless, GL renderer, xwayland, lcms) for shipping in a
#       qdistro VM/image. The system `weston` binary loads this
#       libweston-16.so.0 via LD_LIBRARY_PATH and its backends via
#       WESTON_MODULE_MAP. Installed under $PREFIX (default
#       /tmp/qdwin-libweston-prod-prefix); qdistro's
#       install-vendored-libweston.sh stages that tree into
#       /usr/libexec/qdistro/qdwin-libweston/. See
#       doc/decisions/0001-vendored-libweston-packaging.md.
#
# This is intentionally not wired into qdwin's meson.build because
# (a) it builds a different pinned-version sub-project with its own
# meson, (b) we want the build cache invalidated only when the
# vendored sources change, not on every qdwin rebuild.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/src"

PROFILE="${QDWIN_LIBWESTON_PROFILE:-headless}"

# J29: single source of truth for the libweston major/soname (from ./VERSION).
# shellcheck source=lib-major.sh
. "$HERE/lib-major.sh"

prepare_pinned_git_subproject() {
    local name="$1"
    local wrap="$SRC/subprojects/$name.wrap"
    local dir="$SRC/subprojects/$name"
    local revision want have

    [ -f "$wrap" ] || return 0
    revision=$(sed -n 's/^[[:space:]]*revision[[:space:]]*=[[:space:]]*//p' "$wrap" | head -n1)
    [ -n "$revision" ] || return 0
    [ -d "$dir" ] || return 0

    if [ ! -d "$dir/.git" ]; then
        echo "note: removing non-git subproject $dir so Meson can honor $wrap" >&2
        rm -rf "$dir"
        return 0
    fi

    if want=$(git -C "$dir" rev-parse --verify "$revision^{commit}" 2>/dev/null); then
        have=$(git -C "$dir" rev-parse HEAD 2>/dev/null || true)
        if [ "$have" != "$want" ]; then
            echo "note: resetting subproject $name from ${have:-unknown} to pinned $revision" >&2
            git -C "$dir" checkout -q --detach "$want"
            git -C "$dir" clean -fdx -q
        fi
    else
        echo "note: removing subproject $dir; existing checkout lacks pinned revision $revision" >&2
        rm -rf "$dir"
    fi
}

case "$PROFILE" in
headless)
    BUILD="$SRC/build"
    PREFIX="${QDWIN_LIBWESTON_PREFIX:-/tmp/qdwin-libweston-prefix}"
    MESON_OPTS=(
        -Dbackend-drm=false
        -Dbackend-rdp=false
        -Dbackend-vnc=false
        -Dbackend-pipewire=false
        -Dbackend-wayland=false
        -Dbackend-x11=false
        -Dbackend-headless=true
        -Dbackend-default=headless
        -Drenderer-gl=false
        -Drenderer-vulkan=false
        -Dxwayland=false
        -Dcolor-management-lcms=false
    )
    NINJA_TARGETS=(
        libweston/${LIBWESTON_SONAME}.so.${LIBWESTON_LIBVER}
        libweston/backend-headless/headless-backend.so
    )
    ;;
production)
    # Separate build dir so the production cache does not clobber the
    # headless test cache (different backend set → full reconfigure).
    BUILD="$SRC/build-prod"
    PREFIX="${QDWIN_LIBWESTON_PREFIX:-/tmp/qdwin-libweston-prod-prefix}"
    # Backend set mirrors the qdwin systemd unit's WESTON_MODULE_MAP
    # (drm + pipewire + rdp + wayland + x11 + headless + xwayland + lcms).
    # GL renderer ON for real-GPU hosts (pixman still selectable in
    # weston.ini for virtio-gpu). VNC stays off (unused by qdwin).
    #
    # Each backend defaults ON but can be forced off for host-side build
    # validation on machines that lack a specific devel package, e.g.
    #   QDWIN_LW_PIPEWIRE=false QDWIN_LW_RDP=false ... production build
    # The VM/image deps (scripts/.../install-deps.sh) cover the full set,
    # so leave these unset for a shippable tree.
    MESON_OPTS=(
        -Dbackend-drm="${QDWIN_LW_DRM:-true}"
        -Dbackend-rdp="${QDWIN_LW_RDP:-true}"
        -Dbackend-vnc=false
        -Dbackend-pipewire="${QDWIN_LW_PIPEWIRE:-true}"
        -Dbackend-wayland="${QDWIN_LW_WAYLAND:-true}"
        -Dbackend-x11="${QDWIN_LW_X11:-true}"
        -Dbackend-headless=true
        -Dbackend-default=drm
        -Drenderer-gl="${QDWIN_LW_GL:-true}"
        -Drenderer-vulkan=false
        -Dxwayland="${QDWIN_LW_XWAYLAND:-true}"
        -Dcolor-management-lcms="${QDWIN_LW_LCMS:-true}"
    )
    # Build everything that gets installed for this backend set.
    NINJA_TARGETS=()
    ;;
*)
    echo "error: unknown QDWIN_LIBWESTON_PROFILE='$PROFILE'" \
         "(want 'headless' or 'production')" >&2
    exit 2
    ;;
esac

if [[ ! -d "$SRC" ]]; then
    echo "error: $SRC missing — re-extract weston @ tag $(cat "$HERE/VERSION") into src/" >&2
    exit 1
fi

cd "$SRC"

# Meson prefers an existing subprojects/<name>/ checkout over the .wrap file.
# qci source tarballs can therefore carry a stale display-info checkout from a
# prior local build; Weston 14 needs libdisplay-info < 0.3.0, while a stale
# checkout at newer upstream HEAD reports 0.4.0-dev and makes the DRM backend
# configure fail. Keep the checkout aligned with display-info.wrap before setup.
prepare_pinned_git_subproject "display-info"

# Reuse the existing build dir for fast incremental rebuilds, but ONLY when it
# was configured against *this* source tree. meson bakes the absolute source
# path into the build config, so a build dir produced against a different tree —
# e.g. an in-tree build-prod/ that rode along in a VM source snapshot, or a
# sibling checkout under a different path — leaves ninja unable to regenerate
# (its recorded srcdir no longer exists). That fails the bake, and the caller
# silently degrades to distro libweston. Detect the mismatch and reconfigure
# from scratch; on any doubt, wipe (a clean rebuild is always correct, just
# slower). Delete the build dir by hand to force a profile/option change.
reuse_build=0
if [[ -f "$BUILD/build.ninja" && -f "$BUILD/meson-info/meson-info.json" ]]; then
    if grep -Fq "\"source\": \"$(realpath "$SRC")\"" "$BUILD/meson-info/meson-info.json"; then
        reuse_build=1
    else
        echo "note: libweston build dir $BUILD was configured against a" \
             "different source tree (not $SRC) — wiping and reconfiguring" >&2
    fi
fi
if [[ "$reuse_build" -eq 0 ]]; then
    rm -rf "$BUILD"
    # The trailing -D flags disable optional weston features qdwin does not use
    # so the production tree builds without their devel deps. J29 (weston 16):
    # -Dscreenshare / -Dshell-fullscreen / -Dwcap-decode / -Dremoting / -Dpipewire
    # (the gst plugin) / -Dbackend-drm-screencast-vaapi were REMOVED upstream in
    # 16 (vaapi recorder + screenshare dropped, remoting/pipewire demoted to
    # -Ddeprecated-* and default off), so they no longer need explicit -D…=false.
    # -Dshell-lua is new in 16 (Lua ≥5.4) and off here.
    meson setup "$BUILD" \
        --prefix="$PREFIX" \
        "${MESON_OPTS[@]}" \
        -Dshell-desktop=false \
        -Dshell-ivi=false \
        -Dshell-kiosk=false \
        -Dshell-lua=false \
        -Dimage-jpeg=false \
        -Dimage-webp=false \
        -Dsystemd=false \
        -Dtools= \
        -Dtests=false \
        -Ddemo-clients=false \
        -Dsimple-clients= \
        -Ddoc=false
fi

if [[ ${#NINJA_TARGETS[@]} -gt 0 ]]; then
    ninja -C "$BUILD" "${NINJA_TARGETS[@]}"
else
    ninja -C "$BUILD"
fi

# Install into the build's --prefix. libweston bakes its module-search
# directory in at compile time (= ${prefix}/${libdir}/libweston-16), so
# weston only finds the backends if the install populates that
# directory. We use a writable temp prefix; nothing global is touched.
DESTDIR= ninja -C "$BUILD" install >/dev/null

# meson's default libdir is distro-dependent: lib64 on openSUSE, the arch
# multiarch dir (lib/x86_64-linux-gnu) on Debian/Ubuntu. We deliberately do
# NOT force --libdir, so locate whichever directory the install actually used
# rather than assuming lib64.
CORE_SO=$(ls "$PREFIX"/lib64/${LIBWESTON_SONAME}.so.0* \
             "$PREFIX"/lib/*/${LIBWESTON_SONAME}.so.0* \
             "$PREFIX"/lib/${LIBWESTON_SONAME}.so.0* 2>/dev/null | head -n1 || true)
[[ -n "$CORE_SO" ]] || { echo "error: no ${LIBWESTON_SONAME}.so under $PREFIX after install" >&2; exit 1; }
LIBDIR=$(dirname "$CORE_SO")

echo
echo "Profile:         $PROFILE"
echo "Built libweston: $BUILD/libweston/${LIBWESTON_SONAME}.so.${LIBWESTON_LIBVER}"
echo "Installed under: $PREFIX (libdir: ${LIBDIR#$PREFIX/})"
ls -la "$LIBDIR/${LIBWESTON_SONAME}.so"*
echo "Backends:"
ls -1 "$LIBDIR/${LIBWESTON_SONAME}/"*.so 2>/dev/null | sed 's/^/  /'
echo
if [[ "$PROFILE" == production ]]; then
    echo "Stage into a VM/image with:"
    echo "  QDWIN_LIBWESTON_PREFIX=$PREFIX \\"
    echo "    qdistro/scripts/install/install-vendored-libweston.sh"
else
    echo "Use with:"
    echo "  LD_LIBRARY_PATH=$LIBDIR\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH} <qdwin-launch-cmd>"
fi
