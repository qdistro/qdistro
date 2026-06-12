#!/usr/bin/env bash
# verify-source-manifest.sh — verify a signed qdistro source manifest.
#
# Usage:
#   scripts/install/verify-source-manifest.sh MANIFEST MANIFEST.sig KEYRING \
#                                             [EXPECT_SIGNER]
#
# The KEYRING is a public-key keyring file suitable for gpgv(1), for example
# one produced by:
#   gpg --export <release-key-fingerprint> > qdistro-release-keyring.gpg
#
# This tool is the release-side primitive for R1/R2:
#   1. gpgv verifies the detached OpenPGP signature over MANIFEST.
#   2. If EXPECT_SIGNER is given (a FULL 40-hex OpenPGP fingerprint, with or
#      without a leading 0x), the AUTHORITATIVE signing key reported by gpgv (its
#      VALIDSIG fingerprint) must equal it EXACTLY (case-insensitively). This
#      binds the manifest to a specific published key even if the keyring holds
#      more than one; it does NOT trust any signer= field inside the document.
#      A short key id is rejected (suffix matching is not collision-resistant).
#   3. gen-source-manifest.sh --lint verifies that MANIFEST is exactly the
#      bootstrap-compatible `<repo> <40-hex-sha>[ key=value]...` pin format.
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
    sed -n '2,20p' "$SCRIPT_PATH" | sed 's/^# \{0,1\}//'
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

[ "$#" -ge 3 ] && [ "$#" -le 4 ] || { usage >&2; exit 2; }

MANIFEST="$1"
SIGNATURE="$2"
KEYRING="$3"
EXPECT_SIGNER="${4:-}"

[ -f "$MANIFEST" ] || die "manifest not found: $MANIFEST"
[ -f "$SIGNATURE" ] || die "signature not found: $SIGNATURE"
[ -f "$KEYRING" ] || die "keyring not found: $KEYRING"
command -v gpgv >/dev/null 2>&1 || die "gpgv not found"
[ -x "$LINTER" ] || die "manifest linter not executable: $LINTER"

# Capture gpgv's machine-readable status so we can bind to a specific key.
STATUS="$(mktemp "${TMPDIR:-/tmp}/qd-gpgv-status.XXXXXX")" || die "mktemp failed"
trap 'rm -f "$STATUS"' EXIT
gpgv --keyring "$KEYRING" --status-fd 3 "$SIGNATURE" "$MANIFEST" 3>"$STATUS" >/dev/null

if [ -n "$EXPECT_SIGNER" ]; then
    # gpgv emits `[GNUPG:] VALIDSIG <40hex-fpr> ...` for a good signature. Bind
    # to the FULL fingerprint, compared EXACTLY (case-insensitively): a short
    # key id / arbitrary suffix is NOT collision-resistant and could match a
    # different valid key in a multi-key release keyring, defeating the point.
    want="$EXPECT_SIGNER"
    case "$want" in 0x*) want="${want#0x}" ;; 0X*) want="${want#0X}" ;; esac
    case "$want" in
        *[!0-9A-Fa-f]* | "") die "EXPECT_SIGNER '$EXPECT_SIGNER' must be a 40-hex OpenPGP fingerprint (optionally 0x-prefixed)" ;;
    esac
    [ "${#want}" -eq 40 ] || die "EXPECT_SIGNER '$EXPECT_SIGNER' must be a FULL 40-hex fingerprint, not a ${#want}-char key id (suffix matching is not collision-resistant)"
    fpr="$(awk '/^\[GNUPG:\] VALIDSIG /{print $3; exit}' "$STATUS")"
    [ -n "$fpr" ] || die "no VALIDSIG in gpgv status; cannot confirm signer '$EXPECT_SIGNER'"
    # Exact, case-insensitive full-fingerprint comparison.
    if [ "${want,,}" != "${fpr,,}" ]; then
        die "manifest signed by $fpr, not the expected signer '$EXPECT_SIGNER'"
    fi
fi

"$LINTER" --lint "$MANIFEST" >/dev/null

printf 'verify-source-manifest: OK %s\n' "$MANIFEST"
