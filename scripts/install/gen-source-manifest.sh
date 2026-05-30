#!/usr/bin/env bash
# gen-source-manifest.sh — release/packaging tool that generates a pinned
# source-manifest.txt for qdistro-bootstrap.sh's HARDENED (daily-driver /
# release) profiles.
#
# Background:
#   In hardened profiles qdistro-bootstrap.sh refuses to root-install
#   (`meson install` / root `pip install`) from a source checkout that is not
#   pinned to an exact commit in scripts/install/source-manifest.txt — see
#   verify_repo_pin / manifest_pin in qdistro-bootstrap.sh. The committed
#   manifest ships with every example line COMMENTED OUT on purpose, so an
#   unpinned hardened install FAILS LOUDLY ("no manifest pin") rather than
#   building from an arbitrary HEAD. This tool is the release-side counterpart:
#   it resolves each repo's current HEAD and emits a real, uncommented manifest
#   in EXACTLY the format the bootstrap's parser accepts.
#
# What it does:
#   - Enumerates the qdistro source repos the bootstrap actually pins:
#       qdistro qdwin qdshell qdlocker qdbrowser qdgreeter
#       qterminator qnotebook qfileman
#     (the union of fetch_sources' fatal + optional sets — keep in sync).
#   - For each repo, resolves the current HEAD commit SHA from a sibling
#     checkout under --repo-root (default: parent dir of this repo, matching
#     the bootstrap's REPO_ROOT default), overridable.
#   - Emits `<repo>\t<40-hex-sha>` lines (TAB-separated) plus a generated
#     header comment. The output parses identically through manifest_pin's
#     awk path (whitespace-separated; '#' / blank lines ignored).
#
# Fail-closed invariants (a RELEASE must pin a clean, real tree):
#   - DIRTY repo  -> refuse to emit its pin (error), unless --allow-dirty.
#   - MISSING repo (no checkout / not a git repo) -> error, unless
#     --allow-missing (then the repo is SKIPPED with a stderr note, leaving a
#     hole that the bootstrap will itself reject at install time).
#   - The resolved SHA must be a 40-hex commit object; anything else aborts.
#
# Lint mode (--lint FILE): verify an existing manifest is byte-compatible with
# the bootstrap parser without generating anything — every non-comment,
# non-blank line must be `<repo> <40-hex-sha>` for a known repo, no dup repos.
#
# Usage:
#   scripts/install/gen-source-manifest.sh [--repo-root DIR] [--allow-dirty]
#                                          [--allow-missing] [-o FILE]
#   scripts/install/gen-source-manifest.sh --lint scripts/install/source-manifest.txt
#
# Output goes to stdout by default (so a release pipeline can redirect it to
# scripts/install/source-manifest.txt deliberately); -o/--output writes a file.

set -euo pipefail

SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(dirname "$SCRIPT_PATH")
# scripts/install/<this> -> qdistro/ -> qdistro-org/ (sibling-checkout parent),
# matching qdistro-bootstrap.sh's REPO_ROOT default.
QDISTRO_DIR_DEFAULT=$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd || true)
REPO_ROOT="${QDISTRO_REPO_ROOT:-${QDISTRO_DIR_DEFAULT:+$(dirname "$QDISTRO_DIR_DEFAULT")}}"
REPO_ROOT="${REPO_ROOT:-/opt/qdistro-src}"

# Authoritative repo set — the union of fetch_sources' fatal set
# (qdistro qdwin qdshell) and its optional set (qdlocker qdbrowser qdgreeter
# qterminator qnotebook qfileman) in qdistro-bootstrap.sh. Order is the order
# the bootstrap fetches/verifies them, and the order the committed manifest's
# example block lists them.
QDISTRO_REPOS=(
    qdistro qdwin qdshell
    qdlocker qdbrowser qdgreeter qterminator qnotebook qfileman
)

ALLOW_DIRTY=""
ALLOW_MISSING=""
OUTPUT=""
LINT_FILE=""

err()  { printf 'gen-source-manifest: ERROR: %s\n' "$*" >&2; }
note() { printf 'gen-source-manifest: %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

print_help() {
    awk 'NR==1{next} /^[^#]/{exit} {sub(/^# ?/,""); print}' "$SCRIPT_PATH"
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --repo-root=*)   REPO_ROOT="${1#*=}" ;;
            --repo-root)     shift; REPO_ROOT="${1:-}" ;;
            --allow-dirty)   ALLOW_DIRTY=1 ;;
            --allow-missing) ALLOW_MISSING=1 ;;
            --output=*|-o=*) OUTPUT="${1#*=}" ;;
            --output|-o)     shift; OUTPUT="${1:-}" ;;
            --lint=*)        LINT_FILE="${1#*=}" ;;
            --lint)          shift; LINT_FILE="${1:-}" ;;
            -h|--help)       print_help; exit 0 ;;
            *) die "unknown argument: $1 (try --help)" ;;
        esac
        shift
    done
}

# repo_head_sha <dir> — print the 40-hex HEAD commit SHA of the git checkout at
# <dir>, or fail. Uses `git -C` so cwd never matters.
repo_head_sha() {
    git -C "$1" rev-parse --verify --quiet 'HEAD^{commit}' 2>/dev/null
}

# repo_is_dirty <dir> — true (0) if the working tree has uncommitted changes
# (tracked modifications, staged changes, or untracked-not-ignored files).
repo_is_dirty() {
    [ -n "$(git -C "$1" status --porcelain 2>/dev/null)" ]
}

is_git_checkout() {
    git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1
}

# --- lint mode ----------------------------------------------------------
# Validate that FILE is byte-compatible with the bootstrap's manifest parser:
# each non-comment, non-blank line is `<repo> <40-hex-sha>` for a KNOWN repo,
# with no duplicate repo lines. Comments (^[[:space:]]*#) and blank lines are
# ignored exactly as manifest_pin's awk ignores them.
lint_manifest() {
    local file="$1"
    [ -f "$file" ] || die "--lint: no such file: $file"

    local known=" ${QDISTRO_REPOS[*]} "
    local lineno=0 bad=0 seen=""
    local raw repo sha rest
    while IFS= read -r raw || [ -n "$raw" ]; do
        lineno=$((lineno + 1))
        # Skip blank and comment lines (matches awk /^[[:space:]]*#/).
        if printf '%s' "$raw" | grep -qE '^[[:space:]]*$'; then continue; fi
        if printf '%s' "$raw" | grep -qE '^[[:space:]]*#'; then continue; fi

        # Parse exactly two whitespace-separated fields, like awk's $1/$2 — but
        # reject a stray third field so the line is unambiguous.
        # shellcheck disable=SC2086
        set -- $raw
        repo="${1:-}"; sha="${2:-}"; rest="${3:-}"
        if [ -z "$repo" ] || [ -z "$sha" ] || [ -n "$rest" ]; then
            err "$file:$lineno: not '<repo> <40-hex-sha>': $raw"; bad=1; continue
        fi
        if ! printf '%s' "$sha" | grep -qE '^[0-9a-f]{40}$'; then
            err "$file:$lineno: pin '$sha' is not a 40-hex commit SHA"; bad=1; continue
        fi
        case "$known" in
            *" $repo "*) : ;;
            *) err "$file:$lineno: unknown repo '$repo' (not a qdistro source repo)"; bad=1; continue ;;
        esac
        case " $seen " in
            *" $repo "*) err "$file:$lineno: duplicate pin for repo '$repo'"; bad=1; continue ;;
        esac
        seen="$seen $repo"
    done < "$file"

    if [ "$bad" -ne 0 ]; then
        die "--lint: $file is NOT a valid source manifest"
    fi
    note "--lint: $file OK ($(printf '%s' "$seen" | wc -w) pinned repo line(s))"
}

# --- generate mode ------------------------------------------------------
generate_manifest() {
    [ -d "$REPO_ROOT" ] || die "repo root '$REPO_ROOT' does not exist (use --repo-root DIR)"

    local body="" repo dir sha missing=0
    for repo in "${QDISTRO_REPOS[@]}"; do
        dir="$REPO_ROOT/$repo"
        if [ ! -d "$dir" ] || ! is_git_checkout "$dir"; then
            if [ -n "$ALLOW_MISSING" ]; then
                note "skip $repo: no git checkout at $dir (--allow-missing)"
                missing=1
                continue
            fi
            die "$repo: no git checkout at $dir (supply --repo-root, or --allow-missing to skip)"
        fi

        if repo_is_dirty "$dir"; then
            if [ -z "$ALLOW_DIRTY" ]; then
                die "$repo: working tree at $dir is DIRTY; a release must pin a clean tree (commit/stash, or pass --allow-dirty to override)"
            fi
            note "WARN $repo: working tree is DIRTY but pinning HEAD anyway (--allow-dirty)"
        fi

        sha="$(repo_head_sha "$dir" || true)"
        if ! printf '%s' "$sha" | grep -qE '^[0-9a-f]{40}$'; then
            die "$repo: could not resolve a 40-hex HEAD commit at $dir (got '$sha')"
        fi
        # TAB-separated, exactly what manifest_pin's awk reads as $1/$2.
        body+="$(printf '%s\t%s' "$repo" "$sha")"$'\n'
    done

    [ -n "$body" ] || die "no repos pinned (all missing?); nothing to emit"

    local header
    header=$(cat <<EOF
# qdistro source manifest — pinned commit SHAs for bootstrap source builds.
#
# GENERATED by scripts/install/gen-source-manifest.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
# from sibling checkouts under: $REPO_ROOT
# Do not hand-edit; re-run the generator. Format (TAB/space-separated):
#   <repo>\\t<40-hex-commit-sha>
# Consumed by scripts/install/qdistro-bootstrap.sh (verify_repo_pin) in the
# HARDENED (daily-driver / release) profiles.
EOF
)

    if [ -n "$OUTPUT" ]; then
        printf '%s\n%s' "$header" "$body" > "$OUTPUT"
        note "wrote $OUTPUT"
    else
        printf '%s\n%s' "$header" "$body"
    fi
    if [ "$missing" -eq 1 ]; then
        note "NOTE: one or more repos were skipped (--allow-missing); the resulting manifest is INCOMPLETE and a hardened install will fail closed on the missing repo(s)."
    fi
}

main() {
    parse_args "$@"
    if [ -n "$LINT_FILE" ]; then
        lint_manifest "$LINT_FILE"
        return 0
    fi
    generate_manifest
}

main "$@"
