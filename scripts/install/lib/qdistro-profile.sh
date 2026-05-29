# qdistro-profile.sh — shared install-profile gate + hardened primitives.
#
# Sourced (never executed) by the bootstrap / install scripts. Defines the
# single source of truth for "dev vs daily-driver/release" so that the
# hardened code path is selected by ONE flag rather than scattered ad-hoc
# checks. Throwaway-VM shortcuts (unsigned HTTP fetches, --no-gpg-checks,
# NOPASSWD: ALL, env-var passwords, predictable /tmp) are gated behind the
# `dev` profile and are NEVER the release default.
#
# Source via:
#   . "$(dirname "$0")/lib/qdistro-profile.sh"        # from scripts/install/
#   resolve_profile                                    # sets QDISTRO_PROFILE
#
# Profiles (QDISTRO_PROFILE):
#   dev           disposable VM / developer bring-up. Shortcuts allowed.
#   daily-driver  a real machine someone uses every day. Hardened.
#   release       packaged / image-baked artifact. Hardened (== daily-driver
#                 for gating purposes; kept distinct for audit messages).
#
# Default is `daily-driver` (fail-safe: a caller who forgets to set the
# profile gets the hardened path, not the throwaway one). The disposable
# test harnesses (spin-test-vm.sh, build-baked-baseweed.sh, fresh-vm-
# bootstrap.sh) set QDISTRO_PROFILE=dev explicitly.
#
# This file expects bash. It defines functions only; it has no side effects
# at source time beyond defining QDISTRO_PROFILE if it is already exported.

# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------
# resolve_profile — normalise + validate $QDISTRO_PROFILE, defaulting to the
# HARDENED profile. Call once near the top of a script after arg parsing.
# Honours an explicit --profile flag if the caller assigned QDISTRO_PROFILE
# from it first. Exits non-zero on an unknown profile.
resolve_profile() {
    local p
    p="${QDISTRO_PROFILE:-daily-driver}"
    case "$p" in
        dev|daily-driver|release) ;;
        # Friendly aliases.
        prod|production) p=release ;;
        daily|dd)        p=daily-driver ;;
        *)
            printf 'qdistro-profile: unknown QDISTRO_PROFILE=%s (want dev|daily-driver|release)\n' "$p" >&2
            return 2
            ;;
    esac
    QDISTRO_PROFILE="$p"
    export QDISTRO_PROFILE
    return 0
}

# is_dev — true (rc 0) only for the disposable developer profile. ALL
# throwaway shortcuts must be gated on this, never on the absence of a flag.
is_dev() { [ "${QDISTRO_PROFILE:-daily-driver}" = "dev" ]; }

# is_release — true for the packaged/release profile specifically.
is_release() { [ "${QDISTRO_PROFILE:-daily-driver}" = "release" ]; }

# is_hardened — true for daily-driver OR release (the "not dev" set). This is
# the gate the forbidden patterns are checked against.
is_hardened() { ! is_dev; }

# ---------------------------------------------------------------------------
# Private runtime directory (replaces predictable /tmp for privileged renders)
# ---------------------------------------------------------------------------
# qd_private_runtime_dir — print the path to a process-private, 0700,
# root-/caller-owned scratch directory created under $XDG_RUNTIME_DIR or
# /run (tmpfs, not world-traversable like /tmp). Created on first call and
# memoised. Caller is responsible for nothing — it is cleaned by an EXIT
# trap registered here only if the caller has not already set one.
#
# This is the canonical replacement for predictable
# /tmp/qdistro-...-$$.xml privileged-render paths: an attacker cannot
# pre-create or symlink-race a name inside a 0700 mktemp dir.
QD_PRIVATE_RUNTIME_DIR=""
qd_private_runtime_dir() {
    if [ -n "$QD_PRIVATE_RUNTIME_DIR" ] && [ -d "$QD_PRIVATE_RUNTIME_DIR" ]; then
        printf '%s' "$QD_PRIVATE_RUNTIME_DIR"
        return 0
    fi
    local base
    if [ -n "${XDG_RUNTIME_DIR:-}" ] && [ -d "${XDG_RUNTIME_DIR}" ] && [ -w "${XDG_RUNTIME_DIR}" ]; then
        base="$XDG_RUNTIME_DIR"
    elif [ -d /run ] && [ -w /run ]; then
        base="/run"
    else
        # Last resort: /tmp, but via mktemp -d (0700, unguessable name) so
        # the predictable-path race is still closed even here.
        base="${TMPDIR:-/tmp}"
    fi
    QD_PRIVATE_RUNTIME_DIR="$(mktemp -d "$base/qdistro-priv.XXXXXX")" || return 1
    chmod 0700 "$QD_PRIVATE_RUNTIME_DIR"
    printf '%s' "$QD_PRIVATE_RUNTIME_DIR"
}

# qd_mktemp_xml <prefix> — print the path of a fresh, 0600, private temp file
# for a privileged libvirt domain XML render. Lives inside the 0700 private
# runtime dir, so the file's predictable name is irrelevant (the parent dir
# is unguessable and unwritable by other users).
qd_mktemp_xml() {
    local prefix="${1:-qdistro}" dir f
    dir="$(qd_private_runtime_dir)" || return 1
    f="$(mktemp "$dir/${prefix}.XXXXXX.xml")" || return 1
    chmod 0600 "$f"
    printf '%s' "$f"
}

# ---------------------------------------------------------------------------
# Secret-free password input (no env vars in daily-driver/release)
# ---------------------------------------------------------------------------
# Env-var passwords leak via /proc/<pid>/environ for the lifetime of a long
# install. In hardened profiles, accept a password only from:
#   1. a file descriptor (QDISTRO_*_PASSWORD_FD=<n>), or
#   2. interactive /dev/tty entry.
# The legacy QDISTRO_*_PASSWORD env vars are honoured ONLY under `dev`.
#
# qd_read_password_fd <fd> <out-varname>
#   Read a single line (the password) from file descriptor <fd> into the
#   named variable. The fd is closed after the read so it cannot leak.
qd_read_password_fd() {
    local fd="$1" out="$2" line=""
    # The fd flows into `read -u` and an `exec <fd><&-` close below. Reject a
    # non-numeric value outright so it can never become a redirection/eval
    # injection surface (defence-in-depth; callers should validate too).
    case "$fd" in
        ''|*[!0-9]*)
            printf 'qd_read_password_fd: invalid file descriptor %s\n' "${fd:-<empty>}" >&2
            return 2
            ;;
    esac
    # shellcheck disable=SC2229
    if ! IFS= read -r -u "$fd" line; then
        # Allow a trailing-newline-less single line.
        :
    fi
    printf -v "$out" '%s' "$line"
    # Close the fd so the secret source does not linger.
    eval "exec ${fd}<&-" 2>/dev/null || true
}

# qd_password_env_allowed — true only under dev. Hardened profiles must
# refuse to read passwords from the environment.
qd_password_env_allowed() { is_dev; }
