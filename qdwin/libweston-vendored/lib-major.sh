# Single source of truth for the vendored libweston major version.
#
# J29 (weston 14 -> 16 migration): every hardcoded "libweston-14" in the
# build/install/test toolchain derives from ./VERSION via this helper, so
# bumping VERSION (e.g. 14.0.2 -> 16.0.0) is the ONLY edit needed to move the
# soversion the scripts expect. Source it, then use:
#
#   $LIBWESTON_VERSION   full version    (e.g. 14.0.2)
#   $LIBWESTON_MAJOR     major only      (e.g. 14)
#   $LIBWESTON_SONAME    soname stem     (e.g. libweston-14)
#
# shellcheck shell=bash
_lwv_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
LIBWESTON_VERSION="$(cat "$_lwv_here/VERSION")"
LIBWESTON_MAJOR="${LIBWESTON_VERSION%%.*}"
LIBWESTON_SONAME="libweston-${LIBWESTON_MAJOR}"
# Weston builds the real .so as libweston-<major>.so.0.0.<patch> (its meson
# uses `version: '0.0.@0@'.format(<patch>)`). Derive that suffix from VERSION's
# patch component so the ninja target name tracks a bump (14.0.2 -> .so.0.0.2;
# 16.0.0 -> .so.0.0.0).
LIBWESTON_LIBVER="0.0.${LIBWESTON_VERSION##*.}"
unset _lwv_here
export LIBWESTON_VERSION LIBWESTON_MAJOR LIBWESTON_SONAME LIBWESTON_LIBVER
