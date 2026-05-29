#!/bin/bash
# Stage qdistro's vendored, patched libweston-14 into a VM/image.
#
# Why vendored libweston: qdwin's layer-shell popup parenting relies on
# soft-linked helper symbols that only exist in the patched tree
# (weston_desktop_xdg_popup_attach_layer_parent and friends). Stock
# libweston-14 cannot exercise the get_popup / layer-popup-grab paths,
# so qdshell popups anchored to layer-shell surfaces degrade. Full
# rationale + the rejected alternatives:
#   qdwin/doc/decisions/0001-vendored-libweston-packaging.md
#
# Install vehicle: a self-contained tree under
#   /usr/libexec/qdistro/qdwin-libweston/
# holding lib64/libweston-14.so.0[.x.y] + lib64/libweston-14/*.so
# (the backends). The system `weston` binary loads the core via
# LD_LIBRARY_PATH and the backends via WESTON_MODULE_MAP — both set in
# the noctalia-session.service unit by install-qdwin-session-for-vm.sh.
# /usr/libexec/ is chosen over /usr/lib64 so the vendored .so never
# shadows the distro's libweston-14 for any *other* weston consumer.
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
set -eu

QDWIN_SRC=${1:-/root/qdistro-src/qdwin}
DEST=${DEST:-/usr/libexec/qdistro/qdwin-libweston}
PREFIX=${QDWIN_LIBWESTON_PREFIX:-/tmp/qdwin-libweston-prod-prefix}

BUILD_SCRIPT="$QDWIN_SRC/libweston-vendored/build-libweston.sh"

# 1. Ensure a built production prefix exists. Build it on demand if the
#    caller didn't pre-stage one (and the qdwin source tree is present).
if [ ! -d "$PREFIX/lib64" ]; then
    if [ -x "$BUILD_SCRIPT" ]; then
        echo "vendored libweston prefix '$PREFIX' missing — building (production profile)..."
        QDWIN_LIBWESTON_PROFILE=production \
            QDWIN_LIBWESTON_PREFIX="$PREFIX" \
            bash "$BUILD_SCRIPT"
    else
        echo "ERROR: no built prefix at '$PREFIX' and no build script at" >&2
        echo "       '$BUILD_SCRIPT'. Build the production profile first:" >&2
        echo "       QDWIN_LIBWESTON_PROFILE=production qdwin/libweston-vendored/build-libweston.sh" >&2
        exit 2
    fi
fi

# 2. Sanity: the core .so and at least the drm backend must be present.
#    A headless-only prefix (the test profile) is not shippable.
if ! ls "$PREFIX"/lib64/libweston-14.so.0* >/dev/null 2>&1; then
    echo "ERROR: $PREFIX/lib64 has no libweston-14.so.0 — wrong/incomplete prefix" >&2
    exit 2
fi
if [ ! -f "$PREFIX/lib64/libweston-14/drm-backend.so" ]; then
    echo "ERROR: $PREFIX/lib64/libweston-14/drm-backend.so missing —" >&2
    echo "       this looks like a headless-only (test) build, not production." >&2
    echo "       Rebuild with QDWIN_LIBWESTON_PROFILE=production." >&2
    exit 2
fi

# 3. Verify the vendored core matches the system weston binary's SONAME
#    expectation. The system `weston` links libweston-14.so.0; an ABI
#    (SONAME-major) mismatch would crash at load. We only guard the
#    major SONAME here — the patched tree is pinned to the same upstream
#    14.0.x as the distro package (see libweston-vendored/VERSION).
if command -v weston >/dev/null 2>&1; then
    want=$(objdump -p "$(command -v weston)" 2>/dev/null \
            | sed -n 's/.*NEEDED *\(libweston-[0-9]*\.so\.[0-9]*\).*/\1/p' \
            | head -n1)
    if [ -n "$want" ] && [ ! -e "$PREFIX/lib64/$want" ]; then
        echo "ERROR: system weston needs '$want' but vendored prefix has none" >&2
        ls -1 "$PREFIX"/lib64/libweston-14.so* >&2 || true
        exit 2
    fi
    [ -n "$want" ] && echo "ABI check: system weston needs $want, vendored prefix provides it"
fi

# 4. Stage the lib64 subtree. Replace atomically-ish: build into a
#    temp sibling, then swap, so a partially-copied tree never wins.
#    mkdir -p (not `install -d`) on the parent so we don't try to chmod
#    a pre-existing system dir like /usr/libexec we don't own.
mkdir -p "$(dirname "$DEST")"
TMP="$DEST.new.$$"
rm -rf "$TMP"
install -d -m 0755 "$TMP/lib64/libweston-14"

# Core library + its versioned symlinks.
cp -a "$PREFIX"/lib64/libweston-14.so* "$TMP/lib64/"
# Backends (drm/pipewire/rdp/wayland/x11/headless/...) + xwayland.
cp -a "$PREFIX"/lib64/libweston-14/*.so "$TMP/lib64/libweston-14/" 2>/dev/null || true

# Own as root in the real (root) install; tolerate non-root host
# dry-runs (e.g. CI staging tests) where chown is not permitted.
chown -R root:root "$TMP" 2>/dev/null || \
    echo "NOTE: chown root skipped (not running as root)"
find "$TMP" -type d -exec chmod 0755 {} +
find "$TMP" -type f -exec chmod 0644 {} +

rm -rf "$DEST"
mv "$TMP" "$DEST"

echo "vendored libweston installed under $DEST"
echo "  core:     $(ls -1 "$DEST"/lib64/libweston-14.so.0* | tail -n1)"
echo "  backends: $(ls -1 "$DEST"/lib64/libweston-14/*.so 2>/dev/null | wc -l) module(s)"
ls -1 "$DEST"/lib64/libweston-14/*.so 2>/dev/null | sed 's/^/    /'

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
  Environment=WESTON_MODULE_MAP=drm-backend.so=$DEST/lib64/libweston-14/drm-backend.so;...
install-qdwin-session-for-vm.sh writes both from \$DEST. Verify after start:
  pmap \$(pgrep -x weston) | grep libweston-14.so   # path must be under qdistro/
EOF
