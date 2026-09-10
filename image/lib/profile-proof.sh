#!/usr/bin/env bash
# profile-proof.sh — assert that the profile BAKED INTO the built image is the
# profile the build was ASKED for.
#
# Why this exists. `QDISTRO_PROFILE` defaults to `release` in both
# build-in-vm.sh and config.sh, deliberately: an unqualified build must never
# produce the dev image (default credentials) by accident. The failure mode of
# that safe default is the mirror image, and it is quiet -- a tester build that
# simply forgets to pass `QDISTRO_PROFILE=dev` gets a release-stamped artifact
# and nothing says so. That is not hypothetical: the 0.1.0-20260902 image
# shipped `PROFILE=release` while image/AGENTS.md and todo/iso/13 both recorded
# the tester image as `dev`, and it went unnoticed for the whole life of the
# image. The profile is security-relevant on both sides -- it decides the
# passwordless sudoers rule AND, since the SELinux cmdline landed, whether the
# image boots permissive or enforcing -- so "the artifact says what we asked
# for" is a build gate, not a log line.
#
# The check reads /etc/qdistro/release out of the finished raw (written by
# image/lib/release-stamp.sh from config.sh's validated QDISTRO_IMAGE_PROFILE),
# so it proves the shipped bytes, not the environment the driver thinks it had.

# qdistro_prove_profile <raw> <requested-profile>
# Prints what it found and returns non-zero on any mismatch or unreadable stamp.
qdistro_prove_profile() {
    local raw="$1" want="$2" stamp got
    [ -f "$raw" ]  || { echo "profile-proof: no such raw: $raw" >&2; return 2; }
    case "$want" in
        dev|release) ;;
        *) echo "profile-proof: requested profile must be dev or release, got: '$want'" >&2; return 2 ;;
    esac

    stamp="$(LIBGUESTFS_BACKEND="${LIBGUESTFS_BACKEND:-direct}" \
             guestfish --ro -a "$raw" -i cat /etc/qdistro/release 2>&1)" || {
        echo "profile-proof: could not read /etc/qdistro/release from $raw" >&2
        echo "$stamp" >&2
        return 3
    }

    got="$(printf '%s\n' "$stamp" | sed -n 's/^PROFILE=//p' | head -1)"
    [ -n "$got" ] || {
        echo "profile-proof: /etc/qdistro/release carries no PROFILE= line" >&2
        printf '%s\n' "$stamp" >&2
        return 3
    }

    echo "profile-proof: requested=$want  baked=$got  ($raw)"
    if [ "$got" != "$want" ]; then
        echo "profile-proof: MISMATCH — the image was asked for '$want' but is stamped '$got'." >&2
        echo "profile-proof: the tester image must be built as:  QDISTRO_PROFILE=dev ./build-in-vm.sh" >&2
        echo "profile-proof: an unqualified ./build-in-vm.sh defaults to release ON PURPOSE; pass the profile." >&2
        return 1
    fi
    echo "profile-proof: OK — shipped stamp matches the requested profile"
    return 0
}
