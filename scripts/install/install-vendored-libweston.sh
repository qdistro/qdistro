#!/bin/bash
# Stage qdistro's vendored, patched ${LIBWESTON_SONAME} into a VM/image.
#
# Why vendored libweston: qdwin's layer-shell popup parenting relies on
# soft-linked helper symbols that only exist in the patched tree
# (weston_desktop_xdg_popup_attach_layer_parent and friends). Stock
# ${LIBWESTON_SONAME} cannot exercise the get_popup / layer-popup-grab paths,
# so qdshell popups anchored to layer-shell surfaces degrade. Full
# rationale + the rejected alternatives:
#   qdwin/doc/decisions/0001-vendored-libweston-packaging.md
#
# Install vehicle: a self-contained tree under
#   /usr/libexec/qdistro/qdwin-libweston/
# holding lib64/${LIBWESTON_SONAME}.so.0[.x.y] + lib64/${LIBWESTON_SONAME}/*.so
# (the backends). The system `weston` binary loads the core via
# LD_LIBRARY_PATH and the backends via WESTON_MODULE_MAP — both set in
# the qdwin-compositor.service unit by install-qdwin-session-for-vm.sh.
# /usr/libexec/ is chosen over /usr/lib64 so the vendored .so never
# shadows the distro's ${LIBWESTON_SONAME} for any *other* weston consumer.
#
# Source: the production build profile of qdwin's build-libweston.sh.
#   QDWIN_LIBWESTON_PROFILE=production qdwin/libweston-vendored/build-libweston.sh
# lays a complete tree under its --prefix (default
# /tmp/qdwin-libweston-prod-prefix). This script copies the lib64/
# subtree of that prefix into the install destination.
#
# Args / env:
#   $1                       — qdwin source tree (default
#                              /root/qdistro-src/qdwin). Only used to
#                              auto-build when the prefix is absent.
#   QDWIN_LIBWESTON_PREFIX   — already-built production prefix to copy
#                              from. If unset and absent, this script
#                              runs build-libweston.sh in production mode.
#   DEST                     — install root (default
#                              /usr/libexec/qdistro/qdwin-libweston).
set -euo pipefail

QDWIN_SRC=${1:-/root/qdistro-src/qdwin}
# The system install location. Kept as a named constant because step 3 scopes
# its ABI hard-failure to "DEST is the real system path" (see there).
DEFAULT_DEST=/usr/libexec/qdistro/qdwin-libweston
DEST=${DEST:-$DEFAULT_DEST}
PREFIX=${QDWIN_LIBWESTON_PREFIX:-/tmp/qdwin-libweston-prod-prefix}

BUILD_SCRIPT="$QDWIN_SRC/libweston-vendored/build-libweston.sh"

# J29 (weston 14->16): derive the libweston major/soname from qdwin's single
# source of truth (libweston-vendored/VERSION) so this staging installer tracks
# a version bump with no edits.
#
# When lib-major.sh is unreachable — a wrong/absent $1, or a pre-J29 qdwin
# checkout that predates the helper — do NOT guess a hardcoded major. A wrong
# guess does not fail: it makes find_srclib match a *different*, possibly stale
# core in the same prefix and stage that instead, producing a silently wrong
# tree (this is exactly how the prod-symbols gate staged a leftover
# libweston-14 out of a 16 prefix). Derive the major from the built prefix
# itself — the highest libweston-<N>.so.0* present — so the staged soname
# always matches something real, and hard-fail if the prefix has none either.
_LMAJ="$QDWIN_SRC/libweston-vendored/lib-major.sh"
if [ -r "$_LMAJ" ]; then
    # shellcheck source=/dev/null
    . "$_LMAJ"
else
    _lw_found=$(ls -d "$PREFIX"/lib64/libweston-[0-9]*.so.0* \
                      "$PREFIX"/lib/*/libweston-[0-9]*.so.0* \
                      "$PREFIX"/lib/libweston-[0-9]*.so.0* 2>/dev/null \
                | sed -n 's#.*/libweston-\([0-9][0-9]*\)\.so\.0.*#\1#p' \
                | sort -rn | head -n1 || true)
    if [ -z "$_lw_found" ]; then
        echo "ERROR: cannot determine the vendored libweston major." >&2
        echo "       '$_LMAJ' is not readable (pass the qdwin source tree as \$1)" >&2
        echo "       and no libweston-<N>.so.0* exists under '$PREFIX' to infer it from." >&2
        exit 2
    fi
    LIBWESTON_SONAME="libweston-${_lw_found}"
    echo "NOTE: '$_LMAJ' unreadable; inferred $LIBWESTON_SONAME from $PREFIX." >&2
    echo "      Pass the qdwin source tree as \$1 to use VERSION as the source of truth." >&2
    unset _lw_found
fi

# Locate the source libdir under $PREFIX without assuming lib64: meson installs
# to lib64 on openSUSE but to the arch multiarch dir (lib/x86_64-linux-gnu) on
# Debian/Ubuntu, and build-libweston.sh deliberately does not force --libdir.
# Echoes the directory holding ${LIBWESTON_SONAME}.so.0*, or nothing if not built.
find_srclib() {
    local so
    so=$(ls "$PREFIX"/lib64/${LIBWESTON_SONAME}.so.0* \
            "$PREFIX"/lib/*/${LIBWESTON_SONAME}.so.0* \
            "$PREFIX"/lib/${LIBWESTON_SONAME}.so.0* 2>/dev/null | head -n1 || true)
    [ -n "$so" ] && dirname "$so"
    # "not found" is a valid result (empty output), NOT an error: callers use
    # `SRCLIB=$(find_srclib)` and branch on emptiness to build-on-demand. Without
    # this, the trailing `[ -n "$so" ]` test returns 1 when the prefix is absent,
    # and under the script's `set -e` that bare command-substitution assignment
    # aborts the installer BEFORE the build branch — so the vendored tree never
    # builds. Return 0 so emptiness reaches the caller.
    return 0
}

# 1. Ensure a built production prefix exists. Build it on demand if the
#    caller didn't pre-stage one (and the qdwin source tree is present).
SRCLIB=$(find_srclib)
if [ -z "$SRCLIB" ]; then
    if [ -x "$BUILD_SCRIPT" ]; then
        echo "vendored libweston prefix '$PREFIX' missing — building (production profile)..."
        QDWIN_LIBWESTON_PROFILE=production \
            QDWIN_LIBWESTON_PREFIX="$PREFIX" \
            bash "$BUILD_SCRIPT"
        SRCLIB=$(find_srclib)
    else
        echo "ERROR: no built prefix at '$PREFIX' and no build script at" >&2
        echo "       '$BUILD_SCRIPT'. Build the production profile first:" >&2
        echo "       QDWIN_LIBWESTON_PROFILE=production qdwin/libweston-vendored/build-libweston.sh" >&2
        exit 2
    fi
fi

# 2. Sanity: the core .so and at least the drm backend must be present.
#    A headless-only prefix (the test profile) is not shippable.
if [ -z "$SRCLIB" ] || ! ls "$SRCLIB"/${LIBWESTON_SONAME}.so.0* >/dev/null 2>&1; then
    echo "ERROR: no ${LIBWESTON_SONAME}.so.0 under $PREFIX (lib64 or lib/<arch>) — wrong/incomplete prefix" >&2
    exit 2
fi
if [ ! -f "$SRCLIB/${LIBWESTON_SONAME}/drm-backend.so" ]; then
    echo "ERROR: $SRCLIB/${LIBWESTON_SONAME}/drm-backend.so missing —" >&2
    echo "       this looks like a headless-only (test) build, not production." >&2
    echo "       Rebuild with QDWIN_LIBWESTON_PROFILE=production." >&2
    exit 2
fi

# 3. Verify the vendored core matches the system weston binary's SONAME
#    expectation. The system `weston` frontend loads the core via
#    LD_LIBRARY_PATH; a SONAME-major mismatch is not a load error but a silent
#    ABI mismatch (differing struct layouts) that SIGABRTs the compositor.
#
#    Probe with `ldd`, NOT `objdump -p | NEEDED`: weston's DIRECT NEEDED list
#    is only libexec_weston.so.0 (+libc) — libweston comes in one level down
#    through that frontend library. The previous NEEDED-only scan therefore
#    matched nothing, left $want empty, and skipped this whole check silently,
#    so a 16 tree staged next to a 14 frontend reported success. ldd resolves
#    the dependency transitively.
if command -v weston >/dev/null 2>&1 && command -v ldd >/dev/null 2>&1; then
    # `head -n1` closes the pipe early, which under `pipefail` would make
    # the pipeline fail with SIGPIPE (141) and abort under `set -e`; grab
    # the full list first, then take the first match in the shell.
    #
    # Compare MAJORS, and compare against the major that will actually be
    # STAGED ($LIBWESTON_MAJOR) — not against the contents of $SRCLIB. The
    # old `[ ! -e "$SRCLIB/$want" ]` test asked "does the source prefix hold
    # the file weston wants", which a STALE sibling major left over in a
    # shared prefix satisfies (this very prefix carried a leftover
    # libweston-14 next to the current 16), reporting "provides it" while
    # step 4 stages only $LIBWESTON_SONAME.
    weston_needed=$(ldd "$(command -v weston)" 2>/dev/null \
            | sed -n 's/.*[^a-z]libweston-\([0-9][0-9]*\)\.so\.[0-9]*.*/\1/p') || true
    want=${weston_needed%%$'\n'*}
    if [ -n "$want" ] && [ "$want" != "$LIBWESTON_MAJOR" ]; then
        # FATALITY IS SCOPED TO A REAL INSTALL. When DEST is the system
        # location the staged tree is what the local weston will actually
        # load, so a mismatch is a hard error. When DEST is elsewhere — the
        # prod-symbols gate's throwaway staging dry-run, or staging into an
        # image root — the relevant frontend is the TARGET image's weston (the
        # golden image installs weston + libweston-16, see
        # scripts/vm/install-deps.sh), not this host's. Failing there would
        # only assert "the build host runs the same weston as the image",
        # which is false by design on an openSUSE host shipping weston 14.
        if [ "$DEST" = "$DEFAULT_DEST" ]; then
            echo "ERROR: system weston links libweston-$want but this stages" \
                 "$LIBWESTON_SONAME — an ABI mismatch that would SIGABRT the" \
                 "compositor at startup." >&2
            exit 2
        fi
        echo "NOTE: this host's weston links libweston-$want but the staged" \
             "tree is $LIBWESTON_SONAME." >&2
        echo "      Not fatal: DEST='$DEST' is not the system install path, so" >&2
        echo "      the tree is destined for an image whose weston is" \
             "$LIBWESTON_SONAME." >&2
    elif [ -n "$want" ]; then
        echo "ABI check: system weston links libweston-$want, matching the" \
             "staged $LIBWESTON_SONAME"
    else
        echo "NOTE: could not resolve a libweston SONAME from the weston" \
             "frontend; skipping the ABI check." >&2
    fi
fi

# 4. Stage the lib64 subtree. Replace atomically-ish: build into a
#    temp sibling, then swap, so a partially-copied tree never wins.
#    mkdir -p (not `install -d`) on the parent so we don't try to chmod
#    a pre-existing system dir like /usr/libexec we don't own.
mkdir -p "$(dirname "$DEST")"
TMP="$DEST.new.$$"
rm -rf "$TMP"
install -d -m 0755 "$TMP/lib64/${LIBWESTON_SONAME}"

# Core library + its versioned symlinks. Source libdir is discovered
# (lib64 or lib/<arch>); the staged tree is always lib64 — the layout the
# qdwin runtime's LD path on the (openSUSE) image expects.
cp -a "$SRCLIB"/${LIBWESTON_SONAME}.so* "$TMP/lib64/"
# Backends + the XWayland module. weston's xwayland.so is a libweston module
# living in the SAME ${LIBWESTON_SONAME}/ module dir as the backends (on this
# openSUSE weston 14 layout: /usr/lib64/${LIBWESTON_SONAME}/xwayland.so), so this glob
# stages it too IF the production profile built it. NB: the current vendored
# src/meson.build deliberately skips the weston frontend/xwayland subdir, so a
# stock vendored prefix has NO xwayland.so here — the qdwin session then maps
# the DISTRO xwayland.so (ABI-compatible: same pinned 14.0.x) instead. See
# install-qdwin-session-for-vm.sh's XWayland wiring. Either way XWayland is
# loaded via `[core] xwayland=true` + WESTON_MODULE_MAP, never the fatal
# `modules=xwayland.so` old-style load.
cp -a "$SRCLIB"/${LIBWESTON_SONAME}/*.so "$TMP/lib64/${LIBWESTON_SONAME}/" 2>/dev/null || true

# Bundled libdisplay-info, when the production build vendored it. weston-16
# links libdisplay-info.so.1 (ABI 1, soname libdisplay-info.so.1); the
# distro ships an incompatible libdisplay-info.so.3, so without bundling the
# vendored weston fails to load with "libdisplay-info.so.1: cannot open
# shared object file" and every session start falls back / dies until the
# lib is hand-copied. The production profile builds libdisplay-info as a
# meson subproject and installs it into the prefix libdir alongside the core,
# so copy it from there into the vendored tree (which is on LD_LIBRARY_PATH).
# Tolerate its absence: a profile that links the system libdisplay-info
# (compatible ABI) won't have one here, and that's fine.
if ls "$SRCLIB"/libdisplay-info.so* >/dev/null 2>&1; then
    cp -a "$SRCLIB"/libdisplay-info.so* "$TMP/lib64/"
    echo "  bundled libdisplay-info: $(ls -1 "$TMP"/lib64/libdisplay-info.so.[0-9]* 2>/dev/null | tail -n1)"
fi

# Own as root in the real (root) install; tolerate non-root host
# dry-runs (e.g. CI staging tests) where chown is not permitted.
chown -R root:root "$TMP" 2>/dev/null || \
    echo "NOTE: chown root skipped (not running as root)"
find "$TMP" -type d -exec chmod 0755 {} +
find "$TMP" -type f -exec chmod 0644 {} +

# Swap the new tree into place without ever leaving $DEST absent: move
# any existing tree aside first, then mv the new tree in, then drop the
# old one. If the final mv fails the old tree is restored so weston is
# never left with no libweston at all.
OLD="$DEST.old.$$"
if [ -e "$DEST" ]; then
    rm -rf "$OLD"
    mv "$DEST" "$OLD"
fi
if mv "$TMP" "$DEST"; then
    rm -rf "$OLD"
else
    echo "ERROR: failed to move staged tree into $DEST; restoring previous tree" >&2
    [ -e "$OLD" ] && mv "$OLD" "$DEST"
    rm -rf "$TMP"
    exit 1
fi

echo "vendored libweston installed under $DEST"
# Informational summary only — guard the pipelines so a quirk in the
# staged tree can't abort the (already-successful) install under pipefail.
echo "  core:     $(ls -1 "$DEST"/lib64/${LIBWESTON_SONAME}.so.0* 2>/dev/null | tail -n1 || true)"
echo "  backends: $(ls -1 "$DEST"/lib64/${LIBWESTON_SONAME}/*.so 2>/dev/null | wc -l || true) module(s)"
ls -1 "$DEST"/lib64/${LIBWESTON_SONAME}/*.so 2>/dev/null | sed 's/^/    /' || true

# 5. SELinux: relabel so the loader can mmap+execute the vendored .so
#    under the targeted policy. /usr/libexec is lib_t-adjacent; restorecon
#    is a no-op when SELinux is disabled/permissive but keeps enforcing
#    images correct.
if command -v restorecon >/dev/null 2>&1; then
    restorecon -RF "$DEST" 2>/dev/null || true
fi

cat <<EOF

The qdwin systemd unit must point at this tree:
  Environment=LD_LIBRARY_PATH=$DEST/lib64
  Environment=WESTON_MODULE_MAP=drm-backend.so=$DEST/lib64/${LIBWESTON_SONAME}/drm-backend.so;...
install-qdwin-session-for-vm.sh writes both from \$DEST. Verify after start:
  pmap \$(pgrep -x weston) | grep ${LIBWESTON_SONAME}.so   # path must be under qdistro/
EOF
