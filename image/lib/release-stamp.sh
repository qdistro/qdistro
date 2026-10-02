#!/bin/bash
# release-stamp.sh -- write /etc/qdistro/release from the source manifest
# (todo/iso/14 Phase C). Sourced by image/config.sh inside the kiwi chroot
# and by tests on the host; pure functions, no globals beyond what is passed.
#
# qdistro_write_release <manifest> <os-release> <dest> <version> <profile>
#   manifest    root/root/qdistro-source-manifest as written by build.sh
#               sync_sources: one SNAPSHOT=<YYYYMMDD> line and one
#               "SOURCE qdistro <40-hex commit> <clean|DIRTY diff-sha256=<16 hex> untracked=<n>>"
#               line for the monorepo (one line; before the monorepo migration
#               there were five, one per synced sibling repo)
#   os-release  the image's /etc/os-release; its VERSION_ID must equal
#               <version> or the two identities the image carries disagree
#   dest        the file to write (/etc/qdistro/release in the image)
#   version     kiwi's $kiwi_iversion (config.xml <version>)
#   profile     dev | release
# Prints a reason on stderr and returns 1 on any defect; the caller treats
# that as FATAL: an image that cannot say what went in is not a tester image.
# The writer's SOURCE grammar, shared with image/verify-contents.sh and the
# qci image gate (ci/lib/gates/image.sh) so reader and writer cannot drift.
# qdistro_source_line_ere <repo> -- the anchored ERE for one SOURCE line
qdistro_source_line_ere() {
    printf '^SOURCE %s [0-9a-f]{40} (clean|DIRTY diff-sha256=[0-9a-f]{16} untracked=[0-9]+)$' "$1"
}

# The pre-monorepo writer stamped one SOURCE line per synced sibling repo, in
# this set, each exactly once, with the same per-line grammar.
QDISTRO_LEGACY_SOURCE_REPOS="qdistro qdwin qdshell qdgreeter qdlocker"

# qdistro_file_has_nul <file> -- true when the file contains a NUL byte.
# GNU grep treats such a file as binary: a NUL can end a "line" early for the
# anchored matches and suppresses non-counting output (astra r2), so every
# SOURCE reader/writer here refuses NUL input and matches with LC_ALL=C grep -a.
qdistro_file_has_nul() {
    [ "$(LC_ALL=C tr -dc '\000' < "$1" | wc -c)" -ne 0 ]
}

# qdistro_read_release_source <release-file>
#   Classify the SOURCE lines of an /etc/qdistro/release. Prints ONE line:
#     mono <sha> <clean|DIRTY>     the current writer's single qdistro line
#     legacy <sha> <clean|DIRTY>   the recognised pre-monorepo five-repo schema
#                                  (<sha> is its qdistro line)
#     invalid <reason...>          anything else (missing, unreadable, a
#                                  symlink, duplicate/extra/malformed lines)
#   Returns 0 for mono/legacy, 1 for invalid. Pure; reads only the file.
#   A symlink is refused: on a host-extracted tree it resolves against the
#   HOST's /, not the image.
# _qdistro_source_emit <kind> <file> <sha> <state> -- fail closed: a
# "successful" classification must still carry a 40-hex SHA and a state.
_qdistro_source_emit() {
    # The null OID is well-formed hex but names no commit: build.sh never
    # writes it, so it is a damaged stamp (invalid), not an identity.
    if [[ "$3" =~ ^[0-9a-f]{40}$ ]] && [ "$3" != 0000000000000000000000000000000000000000 ] \
        && [[ "$4" =~ ^(clean|DIRTY)$ ]]; then
        echo "$1 $3 $4"; return 0
    fi
    echo "invalid $2 SOURCE qdistro line did not yield a non-null 40-hex SHA and clean|DIRTY state"
    return 1
}

qdistro_read_release_source() {
    local f="$1" n line sha state repo
    if [ -L "$f" ]; then echo "invalid $f is a symlink"; return 1; fi
    if [ ! -f "$f" ] || [ ! -r "$f" ]; then echo "invalid $f is missing or unreadable"; return 1; fi
    if qdistro_file_has_nul "$f"; then echo "invalid $f contains a NUL byte (binary, not the writer's text)"; return 1; fi
    n="$(LC_ALL=C grep -ac '^SOURCE ' "$f")"
    if [ "$n" -eq 1 ]; then
        line="$(LC_ALL=C grep -a '^SOURCE ' "$f")"
        if ! printf '%s\n' "$line" | LC_ALL=C grep -aqE "$(qdistro_source_line_ere qdistro)"; then
            echo "invalid $f SOURCE line is not 'SOURCE qdistro <40-hex> clean|DIRTY diff-sha256=<16-hex> untracked=<n>'"
            return 1
        fi
        read -r _ _ sha state _ <<<"$line"
        _qdistro_source_emit mono "$f" "$sha" "$state"; return
    fi
    local -a legacy
    read -ra legacy <<<"$QDISTRO_LEGACY_SOURCE_REPOS"
    set -- "${legacy[@]}"
    if [ "$n" -eq "$#" ]; then
        for repo in "$@"; do
            [ "$(LC_ALL=C grep -acE "$(qdistro_source_line_ere "$repo")" "$f")" -eq 1 ] || {
                echo "invalid $f has $n SOURCE lines but not exactly one well-formed line for each of: $QDISTRO_LEGACY_SOURCE_REPOS"
                return 1
            }
        done
        line="$(LC_ALL=C grep -aE "$(qdistro_source_line_ere qdistro)" "$f")"
        read -r _ _ sha state _ <<<"$line"
        _qdistro_source_emit legacy "$f" "$sha" "$state"; return
    fi
    echo "invalid $f has $n SOURCE lines, want 1 (SOURCE qdistro ...)"
    return 1
}

qdistro_write_release() {
    local manifest="$1" osrel="$2" dest="$3" version="$4" profile="$5"
    local snapshot n
    if [ ! -s "$manifest" ]; then
        echo "release-stamp: $manifest missing or empty; build.sh sync_sources must write it" >&2
        return 1
    fi
    if qdistro_file_has_nul "$manifest"; then
        echo "release-stamp: $manifest contains a NUL byte; refusing binary input" >&2
        return 1
    fi
    snapshot="$(sed -n 's/^SNAPSHOT=\([0-9]\{8\}\)$/\1/p' "$manifest" | head -n1)"
    if [ -z "$snapshot" ]; then
        echo "release-stamp: $manifest has no SNAPSHOT=<YYYYMMDD> line" >&2
        return 1
    fi
    # Exactly the one synced monorepo, once, with a 40-hex commit and the
    # writer's clean/DIRTY grammar. Counting generic SOURCE lines would
    # accept a stranger's repo or a "no-git" placeholder (round-1 review).
    n="$(LC_ALL=C grep -ac '^SOURCE ' "$manifest")"
    if [ "$n" -ne 1 ]; then
        echo "release-stamp: $manifest lists $n SOURCE lines, want 1 (the qdistro monorepo)" >&2
        return 1
    fi
    if [ "$(LC_ALL=C grep -acE "$(qdistro_source_line_ere qdistro)" "$manifest")" -ne 1 ]; then
        echo "release-stamp: $manifest lacks exactly one well-formed 'SOURCE qdistro <40-hex> clean|DIRTY diff-sha256=<16-hex> untracked=<n>' line" >&2
        return 1
    fi
    case "$profile" in
        dev|release) ;;
        *) echo "release-stamp: profile must be dev or release, got: $profile" >&2; return 1 ;;
    esac
    case "$version" in
        *[!0-9.]*|"") echo "release-stamp: version must be digits and dots, got: $version" >&2; return 1 ;;
    esac
    if ! grep -q "^VERSION_ID=\"$version\"\$" "$osrel" 2>/dev/null; then
        echo "release-stamp: $osrel VERSION_ID != config.xml <version> $version; bump image/root/etc/os-release.qdistro" >&2
        return 1
    fi
    install -d -m 0755 "$(dirname "$dest")"
    {
        echo "# qdistro image provenance; written by image/config.sh at build time."
        echo "VERSION=$version"
        echo "SNAPSHOT=$snapshot"
        echo "PROFILE=$profile"
        echo "BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "ARTIFACT=qdistro-$version-$snapshot.raw.xz"
        LC_ALL=C grep -a '^SOURCE ' "$manifest"
    } > "$dest"
    chmod 0644 "$dest"
}
