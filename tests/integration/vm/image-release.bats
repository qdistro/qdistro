#!/usr/bin/env bats
# Phase C of todo/iso/14: the tester image's configuration and provenance.
# VM-free and rootless. Pins: config.xml (raw only, 28 GiB, snapshot pin,
# bundle name), build.sh's snapshot parser and source manifest, the
# release-stamp library config.sh uses for /etc/qdistro/release, and the
# verify-contents rows that read it. The build itself is proven by
# image/build-in-vm.sh (release-artifact.txt) and the checklist on the raw.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    IMAGE="$REPO/image"
    [ -f "$IMAGE/config.xml" ]
    [ -f "$IMAGE/build.sh" ]
    [ -f "$IMAGE/lib/release-stamp.sh" ]
    T="$BATS_TEST_TMPDIR"
}

# xml_get <xpath-ish python expr> -- read config.xml with the stdlib parser
xml() { python3 - "$IMAGE/config.xml" "$@" <<'PY'
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
what = sys.argv[2]
t = root.find('preferences/type')
if what == 'type-attr': print(t.get(sys.argv[3], ''))
elif what == 'size': print(t.findtext('size', ''), t.find('size').get('unit', ''))
elif what == 'oem': print(' '.join(sorted(c.tag for c in t.find('oemconfig'))))
elif what == 'repos':
    for r in root.findall('repository'): print(r.get('alias'), r.find('source').get('path'))
elif what == 'version': print(root.findtext('preferences/version'))
PY
}

# Fake sibling layout: $T/tree/{qdistro,qdwin,qdshell,qdgreeter,qdlocker}
# with qdistro/image/ holding a copy of build.sh and a config.xml, so
# build.sh's $HERE/../.. sibling walk and its snapshot parser run for real.
fake_tree() {
    local cfg="${1:-$IMAGE/config.xml}"
    mkdir -p "$T/tree/qdistro/image"
    cp "$IMAGE/build.sh" "$T/tree/qdistro/image/build.sh"
    cp "$cfg" "$T/tree/qdistro/image/config.xml"
    # same ignores as the real image/.gitignore: the sync's own output must
    # not make the qdistro tree look dirty
    printf 'root/root/\nlogs/\n' > "$T/tree/qdistro/image/.gitignore"
    for r in qdwin qdshell qdgreeter qdlocker; do
        mkdir -p "$T/tree/$r"; echo "$r" > "$T/tree/$r/README"
    done
    for r in qdistro qdwin qdshell qdgreeter qdlocker; do
        git -C "$T/tree/$r" init -q
        git -C "$T/tree/$r" -c user.email=t@t -c user.name=t add -A
        git -C "$T/tree/$r" -c user.email=t@t -c user.name=t commit -q -m init
    done
}

@test "config.xml: tester build is the raw alone, 28 GiB, no systemsize cap, bundle-named" {
    [ "$(xml type-attr image)" = oem ]
    [ "$(xml type-attr installiso)" = false ]
    [ "$(xml type-attr bundle_format)" = '%N-%v-%I' ]
    [ "$(xml size)" = "28672 M" ]
    run xml oem
    [[ "$output" != *oem-systemsize* ]]
    [[ "$output" == *oem-swap* ]]
    # oem-resize stays at kiwi's default (runs every boot); the opt-out is
    # not set, so a stick moved to a bigger one grows again.
    [[ "$output" != *oem-resize* ]]
    # kiwi's compressed= is only valid for pxe/kis; the xz step is build.sh's
    [ -z "$(xml type-attr compressed)" ]
}

@test "config.xml: both repositories pin the same Tumbleweed snapshot over https" {
    run xml repos
    [ "${#lines[@]}" -eq 2 ]
    local ids=()
    for l in "${lines[@]}"; do
        [[ "$l" =~ ^Tumbleweed-(OSS|NonOSS)\ https://download\.opensuse\.org/history/([0-9]{8})/tumbleweed/repo/(oss|non-oss)/$ ]]
        ids+=("${BASH_REMATCH[2]}")
    done
    [ "${ids[0]}" = "${ids[1]}" ]
    # and build.sh reads exactly that id from the file
    run bash "$IMAGE/build.sh" --snapshot-id
    [ "$status" -eq 0 ]
    [ "$output" = "${ids[0]}" ]
}

@test "build.sh: --snapshot-id refuses repositories pinned to different snapshots" {
    sed 's|history/\([0-9]\{8\}\)/tumbleweed/repo/non-oss/|history/19990101/tumbleweed/repo/non-oss/|' \
        "$IMAGE/config.xml" > "$T/bad.xml"
    fake_tree "$T/bad.xml"
    run bash "$T/tree/qdistro/image/build.sh" --snapshot-id
    [ "$status" -eq 2 ]
    [[ "$output" == *"pin different snapshots"* ]]
}

@test "build.sh: --snapshot-id refuses an unpinned (rolling) repository" {
    sed 's|https://download.opensuse.org/history/[0-9]\{8\}/tumbleweed/repo/oss/|https://download.opensuse.org/tumbleweed/repo/oss/|' \
        "$IMAGE/config.xml" > "$T/bad.xml"
    fake_tree "$T/bad.xml"
    run bash "$T/tree/qdistro/image/build.sh" --snapshot-id
    [ "$status" -eq 2 ]
    [[ "$output" == *"exactly two repositories"* ]]
}

@test "build.sh: --sync-only writes the source manifest: snapshot + five commits with clean/DIRTY state" {
    fake_tree
    echo dirty >> "$T/tree/qdwin/README"           # tracked change -> DIRTY
    echo new > "$T/tree/qdshell/untracked.txt"     # untracked only -> DIRTY, untracked=1
    rm -rf "$T/tree/qdlocker/.git"                 # no-git
    run bash "$T/tree/qdistro/image/build.sh" --sync-only
    [ "$status" -eq 0 ]
    local m="$T/tree/qdistro/image/root/root/qdistro-source-manifest"
    [ -s "$m" ]
    local snap; snap="$(bash "$IMAGE/build.sh" --snapshot-id)"
    grep -qx "SNAPSHOT=$snap" "$m"
    [ "$(grep -c '^SOURCE ' "$m")" -eq 5 ]
    grep -qE "^SOURCE qdistro [0-9a-f]{40} clean$" "$m"
    grep -qE "^SOURCE qdwin [0-9a-f]{40} DIRTY diff-sha256=[0-9a-f]{16} untracked=0$" "$m"
    grep -qE "^SOURCE qdshell [0-9a-f]{40} DIRTY diff-sha256=[0-9a-f]{16} untracked=1$" "$m"
    grep -qE "^SOURCE qdgreeter [0-9a-f]{40} clean$" "$m"
    grep -qx "SOURCE qdlocker no-git" "$m"
    # the commit recorded is the sibling's HEAD
    grep -q "^SOURCE qdistro $(git -C "$T/tree/qdistro" rev-parse HEAD) " "$m"
    # and the sync itself still lands the sources (with .git stripped)
    [ -f "$T/tree/qdistro/image/root/root/qdistro-src/qdwin/README" ]
    [ ! -e "$T/tree/qdistro/image/root/root/qdistro-src/qdwin/.git" ]
}

@test "build.sh: --no-sync without a manifest is refused (the image could not say what went in)" {
    fake_tree
    run bash "$T/tree/qdistro/image/build.sh" --no-sync
    [ "$status" -eq 2 ]
    [[ "$output" == *"no source manifest"* ]]
}

good_manifest() {
    cat > "$1" <<M
SNAPSHOT=20260902
SOURCE qdistro 1111111111111111111111111111111111111111 clean
SOURCE qdwin 2222222222222222222222222222222222222222 DIRTY diff-sha256=abcdefabcdefabcd untracked=0
SOURCE qdshell 3333333333333333333333333333333333333333 clean
SOURCE qdgreeter 4444444444444444444444444444444444444444 clean
SOURCE qdlocker 5555555555555555555555555555555555555555 clean
M
}

@test "release-stamp: writes /etc/qdistro/release with version, snapshot, profile, artifact name and five sources" {
    source "$IMAGE/lib/release-stamp.sh"
    good_manifest "$T/manifest"
    printf 'NAME="qdistro"\nVERSION_ID="0.1.0"\n' > "$T/os-release"
    run qdistro_write_release "$T/manifest" "$T/os-release" "$T/root/etc/qdistro/release" 0.1.0 dev
    [ "$status" -eq 0 ]
    local f="$T/root/etc/qdistro/release"
    [ "$(stat -c %a "$f")" = 644 ]
    grep -qx "VERSION=0.1.0" "$f"
    grep -qx "SNAPSHOT=20260902" "$f"
    grep -qx "PROFILE=dev" "$f"
    grep -qx "ARTIFACT=qdistro-0.1.0-20260902.raw.xz" "$f"
    grep -qE '^BUILD_DATE=[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$' "$f"
    [ "$(grep -c '^SOURCE ' "$f")" -eq 5 ]
    grep -qx "SOURCE qdwin 2222222222222222222222222222222222222222 DIRTY diff-sha256=abcdefabcdefabcd untracked=0" "$f"
    # shell-sourceable KEY=value lines (like os-release) apart from SOURCE rows
    ( set -e; eval "$(grep -v '^SOURCE \|^#' "$f")"; [ "$SNAPSHOT" = 20260902 ] )
}

@test "release-stamp: refuses a missing manifest, a short one, a version/os-release mismatch and a bad profile" {
    source "$IMAGE/lib/release-stamp.sh"
    printf 'VERSION_ID="0.1.0"\n' > "$T/os-release"
    run qdistro_write_release "$T/none" "$T/os-release" "$T/out" 0.1.0 dev
    [ "$status" -eq 1 ]; [[ "$output" == *"missing or empty"* ]]
    good_manifest "$T/m4"; sed -i '/qdlocker/d' "$T/m4"
    run qdistro_write_release "$T/m4" "$T/os-release" "$T/out" 0.1.0 dev
    [ "$status" -eq 1 ]; [[ "$output" == *"4 SOURCE lines, want 5"* ]]
    good_manifest "$T/m"; sed -i 's/^SNAPSHOT=.*/SNAPSHOT=latest/' "$T/m"
    run qdistro_write_release "$T/m" "$T/os-release" "$T/out" 0.1.0 dev
    [ "$status" -eq 1 ]; [[ "$output" == *"no SNAPSHOT"* ]]
    good_manifest "$T/m"
    run qdistro_write_release "$T/m" "$T/os-release" "$T/out" 0.2.0 dev
    [ "$status" -eq 1 ]; [[ "$output" == *"VERSION_ID != config.xml"* ]]
    run qdistro_write_release "$T/m" "$T/os-release" "$T/out" 0.1.0 hardened
    [ "$status" -eq 1 ]; [[ "$output" == *"profile must be"* ]]
    [ ! -e "$T/out" ]
}

@test "release-stamp: config.xml <version> and the os-release override agree (what the chroot check enforces)" {
    local v; v="$(xml version)"
    grep -qx "VERSION_ID=\"$v\"" "$IMAGE/root/etc/os-release.qdistro"
}

# Minimal image root for the checklist rows added in Phase C; the rest of
# the checklist is allowed to MISS (we grep the rows we own).
fake_root() {
    local profile=$1
    mkdir -p "$T/root/etc/qdistro" "$T/root/etc/systemd/system/multi-user.target.wants" "$T/root/etc/sudoers.d"
    source "$IMAGE/lib/release-stamp.sh"
    good_manifest "$T/manifest"
    printf 'VERSION_ID="0.1.0"\n' > "$T/os-release"
    qdistro_write_release "$T/manifest" "$T/os-release" "$T/root/etc/qdistro/release" 0.1.0 "$profile"
}

@test "verify-contents: provenance row reads /etc/qdistro/release; dev profile requires the sudoers rule" {
    fake_root dev
    echo 'admin ALL=(ALL) NOPASSWD: ALL' > "$T/root/etc/sudoers.d/99-admin"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"OK   image provenance:"*"(0.1.0 20260902 dev)"* ]]
    [[ "$output" == *"OK   dev profile: passwordless sudoers baked"* ]]
    [[ "$output" == *"OK   sshd NOT enabled (multi-user)"* ]]
    [[ "$output" == *"OK   sshd NOT enabled (sockets)"* ]]
}

@test "verify-contents: release profile must NOT carry the sudoers rule; an enabled sshd fails" {
    fake_root release
    echo 'admin ALL=(ALL) NOPASSWD: ALL' > "$T/root/etc/sudoers.d/99-admin"
    ln -s /usr/lib/systemd/system/sshd.service "$T/root/etc/systemd/system/multi-user.target.wants/sshd.service"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL release profile: no passwordless sudoers: must be absent but exists"* ]]
    [[ "$output" == *"FAIL sshd NOT enabled (multi-user): must be absent but exists"* ]]
    rm "$T/root/etc/sudoers.d/99-admin" "$T/root/etc/systemd/system/multi-user.target.wants/sshd.service"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"OK   release profile: no passwordless sudoers"* ]]
}

@test "verify-contents: a missing or truncated /etc/qdistro/release is a MISS, not a pass" {
    mkdir -p "$T/root/etc"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"MISS image provenance:"*"(missing)"* ]]
    fake_root dev
    sed -i '/^SOURCE qdlocker/d;/^SNAPSHOT/d;/^PROFILE/d' "$T/root/etc/qdistro/release"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"MISS image provenance:"*"no SNAPSHOT=YYYYMMDD;"*"no SOURCE qdlocker"* ]]
    # profile unknown -> neither sudoers row is emitted rather than guessed
    [[ "$output" != *"passwordless sudoers"* ]]
}

@test "config.sh: stamps the release file from the synced lib before the installer chain, fatally" {
    local c="$IMAGE/config.sh"
    grep -q '^\. "\$QD/image/lib/release-stamp.sh"$' "$c"
    grep -q 'qdistro_write_release /root/qdistro-source-manifest /etc/os-release' "$c"
    grep -q 'FATAL: could not write /etc/qdistro/release' "$c"
    # ordering: the stamp precedes the chain, so a build that cannot say what
    # went in never gets as far as installing it
    [ "$(grep -n 'qdistro_write_release' "$c" | head -1 | cut -d: -f1)" -lt "$(grep -n '^INSTALLERS=(' "$c" | cut -d: -f1)" ]
    # sshd stays off: nothing in config.sh enables it
    ! grep -qE 'systemctl (enable|start).*sshd' "$c"
    # the dev-profile sudoers warning still prints
    grep -q 'WARN: dev profile' "$c"
}

@test "build-in-vm.sh: proves the release artifact on the host (name, checksum, xz -t, exact size)" {
    local b="$IMAGE/build-in-vm.sh"
    grep -q 'XZ_NAME="qdistro-\$IMAGE_VERSION-\$SNAPSHOT.raw.xz"' "$b"
    grep -q 'copy-out /out/bundle' "$b"
    grep -q 'sha256sum -c "\$XZ_NAME.sha256"' "$b"
    grep -q 'xz -t -T0 "\$host_xz"' "$b"
    grep -q 'decompressed size != <size unit=M>' "$b"
    grep -q 'raw size != <size unit=M>' "$b"
}

@test "ci image gate: with no install ISO the install stages are recorded as skipped, not run" {
    local g="$REPO/ci/lib/gates/image.sh"
    grep -q 'record_skip image install-test.sh image' "$g"
    grep -q 'installiso=false' "$g"
}
