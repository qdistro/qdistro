#!/usr/bin/env bash
# Print the pkgconfig directory of the BUILT vendored libweston prefix.
#
# Why this exists: qdwin is ported to the libweston major pinned in VERSION
# (16), but distros ship an older libweston devel package (openSUSE
# Tumbleweed: 14). So `meson setup` on a stock host cannot resolve
# `libweston-16` from the system pkg-config path, and the vendored production
# prefix — which the CI gate builds anyway for the popup-symbol assertion — is
# the only place a matching libweston-<major>.pc exists.
#
# Callers put this on PKG_CONFIG_PATH:
#
#   d="$(bash libweston-vendored/pkgconfig-dir.sh)" \
#       && export PKG_CONFIG_PATH="$d:${PKG_CONFIG_PATH:-}"
#
# Exits 1 with a message on stderr (and prints nothing on stdout) when the
# prefix has not been built, so a caller can branch on emptiness and fall back
# to the system path rather than exporting a bogus entry.
#
# Env:
#   QDWIN_LIBWESTON_PREFIX  production prefix to inspect
#                           (default /tmp/qdwin-libweston-prod-prefix — the
#                           default build-libweston.sh installs into)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=lib-major.sh
. "$HERE/lib-major.sh"

PREFIX="${QDWIN_LIBWESTON_PREFIX:-/tmp/qdwin-libweston-prod-prefix}"

# The prefix libdir is not assumed: meson installs to lib64 on openSUSE but to
# the arch multiarch dir on Debian/Ubuntu, and build-libweston.sh deliberately
# does not force --libdir. Accept whichever holds the .pc we need.
for d in "$PREFIX"/lib64/pkgconfig \
         "$PREFIX"/lib/*/pkgconfig \
         "$PREFIX"/lib/pkgconfig; do
    if [ -f "$d/$LIBWESTON_SONAME.pc" ]; then
        printf '%s\n' "$d"
        exit 0
    fi
done

echo "pkgconfig-dir: no $LIBWESTON_SONAME.pc under '$PREFIX' — build the" \
     "vendored production profile first:" >&2
echo "  QDWIN_LIBWESTON_PROFILE=production $HERE/build-libweston.sh" >&2
exit 1
