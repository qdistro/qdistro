#!/usr/bin/env bash
# verify-source-manifest.sh — verify a signed qdistro source manifest.
#
# Usage:
#   scripts/install/verify-source-manifest.sh MANIFEST MANIFEST.sig KEYRING
#
# The KEYRING is a public-key keyring file suitable for gpgv(1), for example
# one produced by:
#   gpg --export <release-key-fingerprint> > qdistro-release-keyring.gpg
#
# This tool is the release-side primitive for R1/R2:
#   1. gpgv verifies the detached OpenPGP signature over MANIFEST.
#   2. gen-source-manifest.sh --lint verifies that MANIFEST is exactly the
#      bootstrap-compatible `<repo> <40-hex-sha>` pin format.
#
# Bootstrap integration should call this before any clone/build/install in a
# hardened release profile once the release keyring is published.

set -euo pipefail

SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(dirname "$SCRIPT_PATH")
LINTER="$SCRIPT_DIR/gen-source-manifest.sh"

die() {
    printf 'verify-source-manifest: ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    sed -n '2,18p' "$SCRIPT_PATH" | sed 's/^# \{0,1\}//'
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

[ "$#" -eq 3 ] || { usage >&2; exit 2; }

MANIFEST="$1"
SIGNATURE="$2"
KEYRING="$3"

[ -f "$MANIFEST" ] || die "manifest not found: $MANIFEST"
[ -f "$SIGNATURE" ] || die "signature not found: $SIGNATURE"
[ -f "$KEYRING" ] || die "keyring not found: $KEYRING"
command -v gpgv >/dev/null 2>&1 || die "gpgv not found"
[ -x "$LINTER" ] || die "manifest linter not executable: $LINTER"

gpgv --keyring "$KEYRING" "$SIGNATURE" "$MANIFEST" >/dev/null
"$LINTER" --lint "$MANIFEST" >/dev/null

printf 'verify-source-manifest: OK %s\n' "$MANIFEST"
