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
#       qterminator qnotebook qfileman qdchrome-extension qdfirefox-extension
#     (the union of fetch_sources' fatal + optional sets — keep in sync).
#   - For each repo, resolves the current HEAD commit SHA from a sibling
#     checkout under --repo-root (default: parent dir of this repo, matching
#     the bootstrap's REPO_ROOT default), overridable.
#   - Emits `<repo>\t<40-hex-sha>` lines (TAB-separated) plus a generated
#     header comment. The output parses identically through manifest_pin's
#     awk path (whitespace-separated; '#' / blank lines ignored).
#   - OPTIONALLY annotates each line with whitespace-separated `key=value`
#     release-metadata fields, after the SHA, in any order:
#       tag=<git-tag>        the exact tag on that repo's HEAD (--tags reads
#                            `git describe --exact-match`; --require-tags makes
#                            an untagged HEAD fatal).
#       artifact=<algo:hex>  a built-artifact digest (sha256:<64hex> /
#                            sha512:<128hex>), supplied via --artifact repo=...
#       signer=<id>          the release signer identity (--signer ID), recorded
#                            on every line for the key-custody cross-check.
#     The 2-field `<repo> <sha>` form stays valid: manifest_pin reads only $2,
#     so these extra fields are invisible to the existing bootstrap pin path and
#     are consumed by the new manifest_field reader (verify_repo_pin tag check).
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
# non-blank line must be `<repo> <40-hex-sha>` for a known repo (no dup repos),
# optionally followed by recognized `key=value` fields (tag=/artifact=/signer=)
# with well-formed values and no duplicate key on a line. An unrecognized key or
# a bare (non key=value) trailing field is rejected, fail-closed.
#
# Usage:
#   scripts/install/gen-source-manifest.sh [--repo-root DIR] [--allow-dirty]
#                                          [--allow-missing] [--tags]
#                                          [--require-tags] [--signer ID]
#                                          [--artifact repo=algo:hex]... [-o FILE]
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
# qterminator qnotebook qfileman qdchrome-extension qdfirefox-extension) in
# qdistro-bootstrap.sh. The extension repos are source-only (nothing is built
# or installed from them); they are pinned because v1 users MANUALLY load an
# extension built from that source (doc/browser-extension-install.md), so the
# pin is what makes the loaded artifact traceable to the signed release.
# Order is the order the bootstrap fetches/verifies them, and the order the committed manifest's
# example block lists them.
QDISTRO_REPOS=(
    qdistro qdwin qdshell
    qdlocker qdbrowser qdgreeter qterminator qnotebook qfileman
    qdchrome-extension qdfirefox-extension
)

ALLOW_DIRTY=""
ALLOW_MISSING=""
OUTPUT=""
LINT_FILE=""
EMIT_TAGS=""
REQUIRE_TAGS=""
SIGNER=""
declare -A ARTIFACTS=()

err()  { printf 'gen-source-manifest: ERROR: %s\n' "$*" >&2; }
note() { printf 'gen-source-manifest: %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

# Recognized release-metadata field keys carried after the SHA.
EXT_KEYS=" tag artifact signer "

# ext_field_valid <key> <value> — 0 if <key> is a recognized release-metadata
# field with a well-formed <value>, else 1. Single source of truth shared by
# the linter and (for --signer/--artifact) the generator's input validation.
ext_field_valid() {
    local key="$1" val="$2"
    [ -n "$val" ] || return 1
    case "$key" in
        tag)
            # git-ref-safe-ish: a leading alnum then graph chars, no '..',
            # no leading '-'/'/' (mirrors the bootstrap's BRANCH constraint).
            case "$val" in -*|/*|*..*|*' '*) return 1 ;; esac
            printf '%s' "$val" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._/+-]*$'
            ;;
        artifact)
            # <algo>:<hex>, algo in {sha256,sha512}, hex length matches algo.
            case "$val" in
                sha256:*) printf '%s' "${val#sha256:}" | grep -qE '^[0-9a-f]{64}$' ;;
                sha512:*) printf '%s' "${val#sha512:}" | grep -qE '^[0-9a-f]{128}$' ;;
                *) return 1 ;;
            esac
            ;;
        signer)
            # An identity token with no whitespace/control: a 0x fingerprint /
            # key id, or an RFC822 address. The custody doc fixes the canonical
            # form; here we only fail-close on whitespace/control chars.
            printf '%s' "$val" | grep -qE '^[[:graph:]]+$'
            ;;
        *) return 1 ;;
    esac
}

# add_artifact <repo=algo:hex> — record an artifact digest for the generator.
add_artifact() {
    local spec="$1" repo val
    case "$spec" in
        *=*) repo="${spec%%=*}"; val="${spec#*=}" ;;
        *) die "--artifact: expected repo=algo:hex, got '$spec'" ;;
    esac
    case "$EXT_KEYS" in *" $repo "*) die "--artifact: '$repo' is a field key, not a repo" ;; esac
    case " ${QDISTRO_REPOS[*]} " in
        *" $repo "*) : ;;
        *) die "--artifact: unknown repo '$repo' (not a qdistro source repo)" ;;
    esac
    ext_field_valid artifact "$val" \
        || die "--artifact $repo=$val: not a well-formed algo:hex digest (sha256:<64hex> / sha512:<128hex>)"
    ARTIFACTS["$repo"]="$val"
}

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
            --tags)          EMIT_TAGS=1 ;;
            --require-tags)  EMIT_TAGS=1; REQUIRE_TAGS=1 ;;
            --signer=*)      SIGNER="${1#*=}" ;;
            --signer)        shift; SIGNER="${1:-}" ;;
            --artifact=*)    add_artifact "${1#*=}" ;;
            --artifact)      shift; add_artifact "${1:-}" ;;
            --output=*|-o=*) OUTPUT="${1#*=}" ;;
            --output|-o)     shift; OUTPUT="${1:-}" ;;
            --lint=*)        LINT_FILE="${1#*=}" ;;
            --lint)          shift; LINT_FILE="${1:-}" ;;
            -h|--help)       print_help; exit 0 ;;
            *) die "unknown argument: $1 (try --help)" ;;
        esac
        shift
    done

    if [ -n "$SIGNER" ]; then
        ext_field_valid signer "$SIGNER" \
            || die "--signer '$SIGNER': must be a whitespace-free identity token (0x fingerprint, key id, or address)"
    fi
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
# each non-comment, non-blank line is `<repo> <40-hex-sha>` for a KNOWN repo
# (no duplicate repo lines), optionally followed by recognized `key=value`
# release-metadata fields (tag=/artifact=/signer=) with well-formed values and
# no duplicate key on a line. A bare (non key=value) trailing field or an
# unknown key is rejected. Comments (^[[:space:]]*#) and blank lines are
# ignored exactly as manifest_pin's awk ignores them.
lint_manifest() {
    local file="$1"
    [ -f "$file" ] || die "--lint: no such file: $file"

    local known=" ${QDISTRO_REPOS[*]} "
    local lineno=0 bad=0 seen=""
    local raw repo sha
    while IFS= read -r raw || [ -n "$raw" ]; do
        lineno=$((lineno + 1))
        # Skip blank and comment lines (matches awk /^[[:space:]]*#/).
        if printf '%s' "$raw" | grep -qE '^[[:space:]]*$'; then continue; fi
        if printf '%s' "$raw" | grep -qE '^[[:space:]]*#'; then continue; fi

        # Field 1 (repo) and field 2 (sha) are the bootstrap-compatible core;
        # any further fields are optional key=value release metadata. Split with
        # `read -ra` (NOT `set -- $raw`) so a token like `tag=*` is NOT glob-
        # expanded against the cwd — that would make the lint cwd-dependent and
        # diverge from the bootstrap's awk consumer, which never globs.
        local -a fields
        read -ra fields <<< "$raw"
        repo="${fields[0]:-}"; sha="${fields[1]:-}"
        if [ -z "$repo" ] || [ -z "$sha" ]; then
            err "$file:$lineno: not '<repo> <40-hex-sha> [key=value]...': $raw"; bad=1; continue
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

        # Validate any trailing key=value fields (fields 3+).
        local tok key val seen_keys="" line_bad=0
        for tok in "${fields[@]:2}"; do
            case "$tok" in
                *=*) key="${tok%%=*}"; val="${tok#*=}" ;;
                *) err "$file:$lineno: trailing field '$tok' is not key=value (want tag=/artifact=/signer=)"; line_bad=1; continue ;;
            esac
            case "$EXT_KEYS" in
                *" $key "*) : ;;
                *) err "$file:$lineno: unknown field key '$key=' (want tag=/artifact=/signer=)"; line_bad=1; continue ;;
            esac
            case " $seen_keys " in
                *" $key "*) err "$file:$lineno: duplicate '$key=' field on one line"; line_bad=1; continue ;;
            esac
            seen_keys="$seen_keys $key"
            if ! ext_field_valid "$key" "$val"; then
                err "$file:$lineno: malformed '$key=$val' field value"; line_bad=1; continue
            fi
        done
        [ "$line_bad" -eq 0 ] || { bad=1; continue; }

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

        # Optional release-metadata fields, appended after the SHA as
        # whitespace-separated key=value tokens (invisible to manifest_pin).
        local ext="" tag
        if [ -n "$EMIT_TAGS" ]; then
            tag="$(git -C "$dir" describe --tags --exact-match HEAD 2>/dev/null || true)"
            if [ -n "$tag" ]; then
                ext_field_valid tag "$tag" \
                    || die "$repo: HEAD tag '$tag' is not a manifest-safe tag name"
                ext+=" tag=$tag"
            elif [ -n "$REQUIRE_TAGS" ]; then
                die "$repo: HEAD ($sha) has no exact-match tag (--require-tags); tag the release commit or drop --require-tags"
            fi
        fi
        if [ -n "${ARTIFACTS[$repo]:-}" ]; then
            ext+=" artifact=${ARTIFACTS[$repo]}"
        fi
        if [ -n "$SIGNER" ]; then
            ext+=" signer=$SIGNER"
        fi

        # TAB-separated core (manifest_pin reads $1/$2); extension fields, if
        # any, follow space-separated.
        body+="$(printf '%s\t%s%s' "$repo" "$sha" "$ext")"$'\n'
    done

    [ -n "$body" ] || die "no repos pinned (all missing?); nothing to emit"

    local header
    header=$(cat <<EOF
# qdistro source manifest — pinned commit SHAs for bootstrap source builds.
#
# GENERATED by scripts/install/gen-source-manifest.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
# from sibling checkouts under: $REPO_ROOT
# Do not hand-edit; re-run the generator. Format (TAB/space-separated):
#   <repo>\\t<40-hex-commit-sha>[ tag=<tag>][ artifact=<algo:hex>][ signer=<id>]
# Consumed by scripts/install/qdistro-bootstrap.sh (verify_repo_pin / the
# manifest_field reader) in the HARDENED (daily-driver / release) profiles.
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
