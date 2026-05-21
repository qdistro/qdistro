# spawn-common.sh — shared bash helpers for the tier-N spawn scripts.
#
# Sourced (never executed) by qdistro/tier*/spawn-tier*.sh. Replaces
# the inline copies of these helpers that each spawn script carried
# until 2026-05-21; those copies had already drifted slightly (tier3
# validated the launch token with a regex while every other tier
# used a string-length check, which would have missed a /dev/urandom
# or od malfunction that produced non-hex bytes of the right length).
#
# Source via:
#   _SC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   . "$_SC_DIR/../lib/spawn-common.sh"
#
# This file expects bash 4+ (for [[ =~ ]]).

# gen_launch_token <log-prefix>
#
# Print a 32-lowercase-hex per-spawn launch token on stdout. Exits the
# calling script with status 5 if /dev/urandom or od produce something
# that doesn't match ^[0-9a-f]{32}$ — qdshell's orphan-dir reaper and
# secctx-instance correlators all filter on that shape.
#
# The token is correlation, not auth; entropy quality from /dev/urandom
# is more than enough.
gen_launch_token() {
    local prefix="${1:-spawn}" tok
    tok="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    if ! [[ "$tok" =~ ^[0-9a-f]{32}$ ]]; then
        echo "${prefix}: failed to generate launch token from /dev/urandom" >&2
        exit 5
    fi
    printf '%s' "$tok"
}
