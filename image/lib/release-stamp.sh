#!/bin/bash
# release-stamp.sh -- write /etc/qdistro/release from the source manifest
# (todo/iso/14 Phase C). Sourced by image/config.sh inside the kiwi chroot
# and by tests on the host; pure functions, no globals beyond what is passed.
#
# qdistro_write_release <manifest> <os-release> <dest> <version> <profile>
#   manifest    root/root/qdistro-source-manifest as written by build.sh
#               sync_sources: one SNAPSHOT=<YYYYMMDD> line and one
#               "SOURCE <repo> <40-hex commit> <clean|DIRTY diff-sha256=<16 hex> untracked=<n>>"
#               line per synced repo, five in all
#   os-release  the image's /etc/os-release; its VERSION_ID must equal
#               <version> or the two identities the image carries disagree
#   dest        the file to write (/etc/qdistro/release in the image)
#   version     kiwi's $kiwi_iversion (config.xml <version>)
#   profile     dev | release
# Prints a reason on stderr and returns 1 on any defect; the caller treats
# that as FATAL: an image that cannot say what went in is not a tester image.
qdistro_write_release() {
    local manifest="$1" osrel="$2" dest="$3" version="$4" profile="$5"
    local snapshot n
    if [ ! -s "$manifest" ]; then
        echo "release-stamp: $manifest missing or empty; build.sh sync_sources must write it" >&2
        return 1
    fi
    snapshot="$(sed -n 's/^SNAPSHOT=\([0-9]\{8\}\)$/\1/p' "$manifest" | head -n1)"
    if [ -z "$snapshot" ]; then
        echo "release-stamp: $manifest has no SNAPSHOT=<YYYYMMDD> line" >&2
        return 1
    fi
    # Exactly the five synced repos, each once, each with a 40-hex commit and
    # the writer's clean/DIRTY grammar. Counting generic SOURCE lines would
    # accept a stranger's repo or a "no-git" placeholder (round-1 review).
    n="$(grep -c '^SOURCE ' "$manifest")"
    if [ "$n" -ne 5 ]; then
        echo "release-stamp: $manifest lists $n SOURCE lines, want 5" >&2
        return 1
    fi
    for repo in qdistro qdwin qdshell qdgreeter qdlocker; do
        if [ "$(grep -cE "^SOURCE $repo [0-9a-f]{40} (clean|DIRTY diff-sha256=[0-9a-f]{16} untracked=[0-9]+)$" "$manifest")" -ne 1 ]; then
            echo "release-stamp: $manifest lacks exactly one well-formed 'SOURCE $repo <40-hex> clean|DIRTY diff-sha256=<16-hex> untracked=<n>' line" >&2
            return 1
        fi
    done
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
        grep '^SOURCE ' "$manifest"
    } > "$dest"
    chmod 0644 "$dest"
}
