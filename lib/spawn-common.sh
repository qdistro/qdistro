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

# domain_xml_tmpfile <log-prefix>
#
# Print the path of a fresh, 0600, private temp file to render a privileged
# libvirt domain XML into. Replaces the predictable
# /tmp/qdistro-tierN-<vm>-$$.xml paths the spawn scripts used until
# 2026-05-29: a predictable name under world-writable /tmp let a local
# attacker pre-create or symlink-race the file before `virsh define` read
# it (the render runs privileged via run_as_admin). The replacement is a
# mktemp'd file inside a 0700 process-private directory under $XDG_RUNTIME_DIR
# (tmpfs) or /run — both per-user and not world-traversable, so neither the
# file nor its parent can be raced by another uid.
#
# Falls back to a 0700 mktemp dir under $TMPDIR/tmp only if neither
# $XDG_RUNTIME_DIR nor /run is writable; even then the unguessable 0700
# parent closes the race. The directory is registered for cleanup via an
# EXIT trap the FIRST time this is called; the file itself is also removed
# by the caller's existing `rm -f "$TMP_XML"` lines (harmless double-remove).
#
# The spawn scripts run privileged (root, via pkexec) but hand the rendered
# XML to `virsh` running as the unprivileged admin user (run_as_admin).
# Pass the admin user/runtime as $2/$3 so the private dir is created under
# the admin's own /run/user/<uid> (tmpfs, 0700) and chowned to admin: the
# admin can then read the file, while no OTHER uid can traverse the dir.
#   $1 = log/file prefix (e.g. qdistro-tier5)
#   $2 = owner user that will read the file via virsh (default: current user)
#   $3 = that owner's runtime dir, used as the private base (default:
#        $XDG_RUNTIME_DIR -> /run -> $TMPDIR)
#
# Exits the calling script with status 5 if a private dir/file cannot be
# created — a privileged render must never fall back to a predictable path.
_QD_DOMAIN_XML_DIR=""
domain_xml_tmpfile() {
    local prefix="${1:-qdistro}" owner="${2:-}" owner_rt="${3:-}" base f
    if [ -z "$_QD_DOMAIN_XML_DIR" ] || [ ! -d "$_QD_DOMAIN_XML_DIR" ]; then
        if [ -n "$owner_rt" ] && [ -d "$owner_rt" ]; then
            base="$owner_rt"
        elif [ -n "${XDG_RUNTIME_DIR:-}" ] && [ -d "${XDG_RUNTIME_DIR}" ] && [ -w "${XDG_RUNTIME_DIR}" ]; then
            base="$XDG_RUNTIME_DIR"
        elif [ -d /run ] && [ -w /run ]; then
            base="/run"
        else
            base="${TMPDIR:-/tmp}"
        fi
        _QD_DOMAIN_XML_DIR="$(mktemp -d "$base/qdistro-domxml.XXXXXX")" || {
            echo "${prefix}: failed to create private domain-XML dir under $base" >&2
            exit 5
        }
        chmod 0700 "$_QD_DOMAIN_XML_DIR"
        # Owned by the reader (admin) so it can traverse its own 0700 dir;
        # still unreadable / un-pre-creatable by any other uid.
        if [ -n "$owner" ]; then
            chown "$owner" "$_QD_DOMAIN_XML_DIR" 2>/dev/null || true
        fi
        # Clean the private dir on exit without clobbering an existing trap.
        local _existing
        _existing="$(trap -p EXIT | sed "s/^trap -- '//;s/' EXIT$//")"
        # shellcheck disable=SC2064
        trap "rm -rf '$_QD_DOMAIN_XML_DIR' 2>/dev/null; ${_existing}" EXIT
    fi
    f="$(mktemp "$_QD_DOMAIN_XML_DIR/${prefix}.XXXXXX.xml")" || {
        echo "${prefix}: failed to create private domain-XML file" >&2
        exit 5
    }
    chmod 0600 "$f"
    [ -n "$owner" ] && chown "$owner" "$f" 2>/dev/null || true
    printf '%s' "$f"
}
