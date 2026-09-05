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
    # a linked worktree: .git is a FILE, and it is still a checkout with a
    # commit (round-1 review: it used to be stamped no-git)
    mv "$T/tree/qdlocker" "$T/main-qdlocker"
    git -C "$T/main-qdlocker" worktree add -q "$T/tree/qdlocker" -b linked
    [ -f "$T/tree/qdlocker/.git" ]
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
    grep -q "^SOURCE qdlocker $(git -C "$T/tree/qdlocker" rev-parse HEAD) clean$" "$m"
    # the commit recorded is the sibling's HEAD
    grep -q "^SOURCE qdistro $(git -C "$T/tree/qdistro" rev-parse HEAD) " "$m"
    # and the sync itself still lands the sources (with .git stripped)
    [ -f "$T/tree/qdistro/image/root/root/qdistro-src/qdwin/README" ]
    [ ! -e "$T/tree/qdistro/image/root/root/qdistro-src/qdwin/.git" ]
    [ ! -e "$T/tree/qdistro/image/root/root/qdistro-src/qdlocker/.git" ]
}

@test "build.sh: a sibling that is not its own git checkout refuses the sync (no 'no-git' placeholder)" {
    fake_tree
    rm -rf "$T/tree/qdlocker/.git"
    run bash "$T/tree/qdistro/image/build.sh" --sync-only
    [ "$status" -eq 2 ]
    [[ "$output" == *"qdlocker is not a git checkout with a commit at HEAD"* ]]
    [ ! -e "$T/tree/qdistro/image/root/root/qdistro-source-manifest" ]
    [ ! -e "$T/tree/qdistro/image/root/root/qdistro-source-manifest.tmp" ]
    # a plain directory INSIDE another repository must not borrow that
    # repository's commit either
    rm -rf "$T/tree"; fake_tree
    rm -rf "$T/tree/qdlocker/.git"
    git -C "$T/tree" init -q; git -C "$T/tree" -c user.email=t@t -c user.name=t add -A qdlocker
    git -C "$T/tree" -c user.email=t@t -c user.name=t commit -q -m outer
    run bash "$T/tree/qdistro/image/build.sh" --sync-only
    [ "$status" -eq 2 ]
    [[ "$output" == *"qdlocker is not a git checkout"* ]]
}

@test "build.sh: a tree with thousands of untracked files is DIRTY, not clean (SIGPIPE under pipefail)" {
    fake_tree
    mkdir -p "$T/tree/qdwin/many"
    (cd "$T/tree/qdwin/many" && seq 1 12000 | xargs touch)
    run bash "$T/tree/qdistro/image/build.sh" --sync-only
    [ "$status" -eq 0 ]
    grep -qE "^SOURCE qdwin [0-9a-f]{40} DIRTY diff-sha256=[0-9a-f]{16} untracked=1$" \
        "$T/tree/qdistro/image/root/root/qdistro-source-manifest"   # one untracked dir
}

@test "build.sh: a failing git status refuses the sync instead of reading as clean" {
    fake_tree
    mkdir -p "$T/shim"
    cat > "$T/shim/git" <<SH
#!/bin/bash
for a in "\$@"; do [ "\$a" = status ] && { echo "fatal: simulated index corruption" >&2; exit 128; }; done
exec /usr/bin/git "\$@"
SH
    chmod +x "$T/shim/git"
    PATH="$T/shim:$PATH" run bash "$T/tree/qdistro/image/build.sh" --sync-only
    [ "$status" -eq 2 ]
    [[ "$output" == *"git status failed"*"simulated index corruption"* ]]
    [ ! -e "$T/tree/qdistro/image/root/root/qdistro-source-manifest" ]
    [ ! -e "$T/tree/qdistro/image/root/root/qdistro-source-manifest.tmp" ]
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

@test "release-stamp: five lines is not enough -- each expected repo once, 40-hex commit, exact DIRTY grammar" {
    source "$IMAGE/lib/release-stamp.sh"
    printf 'VERSION_ID="0.1.0"\n' > "$T/os-release"
    # a stranger's repo in place of qdlocker
    good_manifest "$T/m"; sed -i 's/^SOURCE qdlocker/SOURCE stranger/' "$T/m"
    run qdistro_write_release "$T/m" "$T/os-release" "$T/out" 0.1.0 dev
    [ "$status" -eq 1 ]; [[ "$output" == *"SOURCE qdlocker"* ]]
    # duplicate repo
    good_manifest "$T/m"; sed -i 's/^SOURCE qdlocker/SOURCE qdwin/' "$T/m"
    run qdistro_write_release "$T/m" "$T/os-release" "$T/out" 0.1.0 dev
    [ "$status" -eq 1 ]
    # the old no-git placeholder
    good_manifest "$T/m"; sed -i 's/^SOURCE qdlocker .*/SOURCE qdlocker no-git/' "$T/m"
    run qdistro_write_release "$T/m" "$T/os-release" "$T/out" 0.1.0 dev
    [ "$status" -eq 1 ]; [[ "$output" == *"SOURCE qdlocker"* ]]
    # malformed DIRTY data
    good_manifest "$T/m"; sed -i 's/^SOURCE qdwin \(.*\) DIRTY.*/SOURCE qdwin \1 DIRTY/' "$T/m"
    run qdistro_write_release "$T/m" "$T/os-release" "$T/out" 0.1.0 dev
    [ "$status" -eq 1 ]; [[ "$output" == *"SOURCE qdwin"* ]]
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

# chain_root <profile> [<state-content>] -- fake_root plus the Phase D rows'
# inputs: the chain record (default: exactly what the bootstrap expects for
# the profile) and the artefacts of the once-missing steps.
chain_root() {
    local profile=$1
    fake_root "$profile"
    mkdir -p "$T/root/var/lib/qdistro/bootstrap"
    if [ $# -ge 2 ]; then
        printf '%s\n' "$2" > "$T/root/var/lib/qdistro/bootstrap/installer-chain.state"
    else
        QDISTRO_PROFILE="$profile" bash -c '. "$1"; resolve_profile >/dev/null; chain_expected_names' _ \
            "$REPO/scripts/install/qdistro-bootstrap.sh" > "$T/root/var/lib/qdistro/bootstrap/installer-chain.state"
    fi
    mkdir -p "$T/root/etc/sysconfig"; printf 'FILTER_RPC_ARGS=""\n' > "$T/root/etc/sysconfig/qemu-ga"
    mkdir -p "$T/root/etc/tmpfiles.d" "$T/root/usr/local/bin" "$T/root/usr/local/lib/qdistro" \
        "$T/root/usr/share/polkit-1/actions" "$T/root/usr/share/qdistro/tier4-vm" \
        "$T/root/usr/share/qdistro/tier5" "$T/root/usr/share/qdistro/tier5b" \
        "$T/root/usr/lib/python3.13/site-packages/qdistro_app" "$T/root/root/qdistro-src/qdistro/tier3"
    : > "$T/root/usr/lib/python3.13/site-packages/qdistro_app/__init__.py"
    : > "$T/root/root/qdistro-src/qdistro/tier3/spawn-tier3.sh"; : > "$T/root/root/qdistro-src/qdistro/tier3/qdistro-tier3-cleanup.sh"
    ln -sfn /root/qdistro-src/qdistro/tier3/spawn-tier3.sh "$T/root/usr/local/bin/qdistro-tier3-spawn"
    ln -sfn /root/qdistro-src/qdistro/tier3/qdistro-tier3-cleanup.sh "$T/root/usr/local/bin/qdistro-tier3-cleanup"
    : > "$T/root/usr/local/lib/qdistro/spawn-common.sh"; : > "$T/root/etc/tmpfiles.d/qdistro-tier3.conf"
    : > "$T/root/usr/share/polkit-1/actions/org.qdistro.tier3.policy"; : > "$T/root/usr/share/polkit-1/actions/org.qdistro.tier5.policy"
    printf 'qdistro-tier3:x:499:admin,user1,user2\n' > "$T/root/etc/group"
    printf 'user1:x:1002:100::/home/user1:/bin/bash\nuser2:x:1003:100::/home/user2:/bin/bash\n' > "$T/root/etc/passwd"
    printf 'user1:!:20000::::::\nuser2:!:20000::::::\n' > "$T/root/etc/shadow"
    local f
    for f in tier4_control.py tier4_chrome.py tier4_publisher_identity.py; do : > "$T/root/usr/share/qdistro/tier4-vm/$f"; done
    for f in tier5-spawn tier5-cleanup tier5-build-guest-image tier5b-spawn tier5b-cleanup tier5b-build-guest-image; do : > "$T/root/usr/local/bin/qdistro-$f"; done
    : > "$T/root/usr/share/qdistro/tier5/domain-template.xml"; : > "$T/root/usr/share/qdistro/tier5b/domain-template.xml"
}

@test "verify-contents: Phase D rows -- the once-missing steps' artefacts and the chain record all pass on a complete tree" {
    chain_root dev
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"OK   [sdk] qdistro_app package"* ]]
    [[ "$output" == *"OK   [tier3] spawn helper"* ]]
    [[ "$output" == *"OK   [tier3] user1 password locked"* ]]
    [[ "$output" == *"OK   [tier3] admin in group"* ]]
    [[ "$output" == *"OK   [tier4-host] control script"* ]]
    [[ "$output" == *"OK   [tier5] polkit action"* ]]
    [[ "$output" == *"OK   [tier5b] domain template"* ]]
    [[ "$output" == *"OK   [qemu-ga] guest-exec allowed"* ]]
    [[ "$output" == *"OK   [chain] record equals the bootstrap chain (dev profile, 16 steps): sdk broker"*"phone"*"tier5b"* ]]
    [[ "$output" == *"OK   [media] socket unit not shipped: absent as required"* ]]
    [[ "$output" == *"OK   [multimachine] broker CLI not shipped: absent as required"* ]]
    # dev: phone rows are requirements (the fixture has no phone unit, so MISS)
    [[ "$output" == *"MISS [phone] unit (dev profile)"* ]]
    [[ "$output" != *"[phone] unit not shipped"* ]]
}

@test "verify-contents: Phase D rows -- release profile expects 15 steps and NO phone; media present is a FAIL" {
    chain_root release
    mkdir -p "$T/root/etc/systemd/system"; : > "$T/root/etc/systemd/system/qdistro-media-exec.socket"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"OK   [chain] record equals the bootstrap chain (release profile, 15 steps):"* ]]
    [[ "$output" != *"(release profile, 15 steps):"*"phone"* ]]
    [[ "$output" == *"OK   [phone] unit not shipped (release profile): absent as required"* ]]
    [[ "$output" == *"FAIL [media] socket unit not shipped: must be absent but exists"* ]]
    [ "$status" -eq 1 ]
    # a release image that carries phone fails
    : > "$T/root/etc/systemd/system/qdistro-phone.service"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"FAIL [phone] unit not shipped (release profile): must be absent but exists"* ]]
}

@test "verify-contents: chain record -- a short, reordered, extra-step or missing record is a MISS with the diff" {
    # short: tier4-host never recorded
    chain_root dev "$(QDISTRO_PROFILE=dev bash -c '. "$1"; resolve_profile >/dev/null; chain_expected_names' _ "$REPO/scripts/install/qdistro-bootstrap.sh" | grep -vx tier4-host)"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [ "$status" -eq 1 ]
    [[ "$output" == *"MISS [chain] record differs from the bootstrap chain (dev profile):"* ]]
    [[ "$output" == *"< tier4-host"* ]]
    # dev record on a release image (phone recorded where it must not be)
    chain_root release "$(QDISTRO_PROFILE=dev bash -c '. "$1"; resolve_profile >/dev/null; chain_expected_names' _ "$REPO/scripts/install/qdistro-bootstrap.sh")"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"MISS [chain] record differs from the bootstrap chain (release profile):"* ]]
    [[ "$output" == *"> phone"* ]]
    # an unknown step recorded
    chain_root dev "$(printf 'sdk\nbogus\n')"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"> bogus"* ]]
    # no record at all
    chain_root dev; rm "$T/root/var/lib/qdistro/bootstrap/installer-chain.state"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"MISS [chain] record: "*"installer-chain.state absent"* ]]
    # blank lines and comments in the record are tolerated
    chain_root dev "$(printf '# written by config.sh\n\n%s\n' "$(QDISTRO_PROFILE=dev bash -c '. "$1"; resolve_profile >/dev/null; chain_expected_names' _ "$REPO/scripts/install/qdistro-bootstrap.sh")")"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"OK   [chain] record equals the bootstrap chain (dev profile, 16 steps)"* ]]
}

@test "verify-contents: tier-3 content rows -- unlocked silo password or admin outside the group is a MISS" {
    chain_root dev
    printf 'user1:$6$abc:20000::::::\nuser2:!:20000::::::\n' > "$T/root/etc/shadow"
    printf 'qdistro-tier3:x:499:user1,user2\n' > "$T/root/etc/group"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"MISS [tier3] user1 password locked"* ]]
    [[ "$output" == *"OK   [tier3] user2 password locked"* ]]
    [[ "$output" == *"MISS [tier3] admin in group"* ]]
    [[ "$output" == *"OK   [tier3] group exists"* ]]
    # the qemu-ga row wants the filter CLEARED, not merely present
    printf 'FILTER_RPC_ARGS="--block-rpcs=guest-exec,guest-exec-status"\n' > "$T/root/etc/sysconfig/qemu-ga"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"MISS [qemu-ga] guest-exec allowed"* ]]
}

@test "verify-contents: a missing or truncated /etc/qdistro/release is a MISS, not a pass" {
    mkdir -p "$T/root/etc"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"MISS image provenance:"*"(missing)"* ]]
    fake_root dev
    sed -i '/^SOURCE qdlocker/d;/^SNAPSHOT/d;/^PROFILE/d' "$T/root/etc/qdistro/release"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"MISS image provenance:"*"no single SNAPSHOT=YYYYMMDD;"*"no single well-formed SOURCE qdlocker line"* ]]
    # profile unknown -> neither sudoers row is emitted rather than guessed
    [[ "$output" != *"passwordless sudoers"* ]]
}

@test "verify-contents: provenance fields must agree with each other (artifact name, duplicates, DIRTY grammar, repo set)" {
    local f="$T/root/etc/qdistro/release"
    fake_root dev; echo x > "$T/root/etc/sudoers.d/99-admin"
    sed -i 's/^ARTIFACT=.*/ARTIFACT=qdistro-9.9.9-19990101.raw.xz/' "$f"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"MISS image provenance:"*"ARTIFACT != qdistro-<VERSION>-<SNAPSHOT>.raw.xz;"* ]]
    fake_root dev; echo "PROFILE=release" >> "$f"          # duplicate key
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"MISS image provenance:"*"no single PROFILE=dev|release;"* ]]
    [[ "$output" != *"passwordless sudoers"* ]]
    fake_root dev; sed -i 's/^\(SOURCE qdwin [0-9a-f]*\) DIRTY.*/\1 DIRTY/' "$f"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"MISS image provenance:"*"no single well-formed SOURCE qdwin line;"* ]]
    fake_root dev; sed -i 's/^SOURCE qdlocker/SOURCE stranger/' "$f"
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"MISS image provenance:"*"no single well-formed SOURCE qdlocker line;"* ]]
    fake_root dev; sed -i 's/^SOURCE qdlocker/SOURCE qdwin/' "$f"  # five lines, one repo twice
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"MISS image provenance:"*"SOURCE qdwin line;"*"SOURCE qdlocker line;"* ]]
    fake_root dev
    run bash "$IMAGE/verify-contents.sh" "$T/root"
    [[ "$output" == *"OK   image provenance:"* ]]
}

@test "config.sh: stamps the release file from the synced lib before the installer chain, fatally" {
    local c="$IMAGE/config.sh"
    grep -q '^\. "\$QD/image/lib/release-stamp.sh"$' "$c"
    grep -q 'qdistro_write_release /root/qdistro-source-manifest /etc/os-release' "$c"
    grep -q 'FATAL: could not write /etc/qdistro/release' "$c"
    # ordering: the stamp precedes the chain, so a build that cannot say what
    # went in never gets as far as installing it
    [ "$(grep -n 'qdistro_write_release' "$c" | head -1 | cut -d: -f1)" -lt "$(grep -n '^install_python_modules$' "$c" | cut -d: -f1)" ]
    # sshd stays off: nothing in config.sh enables it
    ! grep -qE 'systemctl (enable|start).*sshd' "$c"
    # the dev-profile sudoers warning still prints
    grep -q 'WARN: dev profile' "$c"
    # one profile gate: validated once, before anything reads it, and the
    # stamp receives the validated value (round-1 review)
    grep -q 'QDISTRO_PROFILE must be dev or release' "$c"
    [ "$(grep -n 'QDISTRO_PROFILE must be dev or release' "$c" | cut -d: -f1)" -lt "$(grep -n 'qdistro_write_release' "$c" | head -1 | cut -d: -f1)" ]
    grep -q '/etc/qdistro/release "\$kiwi_iversion" "\$QDISTRO_IMAGE_PROFILE"' "$c"
    ! grep -q 'hardened profile' "$c"
}

# A 1 MiB "raw" (half random, half zeros) bundled the way kiwi does it:
# xz -T0 in place under the release name, sha256 of the compressed file.
fixture_bundle() {
    local name=$1
    mkdir -p "$T/b"
    { head -c 524288 /dev/urandom; head -c 524288 /dev/zero; } > "$T/raw"
    cp "$T/raw" "$T/b/${name%.xz}"
    xz -T0 -f "$T/b/${name%.xz}"
    (cd "$T/b" && sha256sum "$name" > "$name.sha256")
}

@test "release-proof: passes a well-formed artifact and reports the evidence" {
    source "$IMAGE/lib/release-proof.sh"
    fixture_bundle qdistro-0.1.0-20260902.raw.xz
    run qdistro_prove_release "$T/raw" "$T/b" qdistro-0.1.0-20260902.raw.xz 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"raw: $T/raw 1048576 bytes (want 1048576)"* ]]
    [[ "$output" == *": OK"* ]]                      # sha256sum -c line
    [[ "$output" == *"xz uncompressed: 1048576 bytes (want 1048576)"* ]]
    [[ "$output" == *"xz -t: OK"* ]]
    [ "${lines[-1]}" = "RESULT: PASS qdistro-0.1.0-20260902.raw.xz" ]
}

@test "release-proof: every check fails closed, and a failure returns (does not exit) to the caller" {
    source "$IMAGE/lib/release-proof.sh"
    local n=qdistro-0.1.0-20260902.raw.xz
    fixture_bundle $n
    # declared size disagrees with the raw
    run qdistro_prove_release "$T/raw" "$T/b" $n 2
    [ "$status" -eq 1 ]; [[ "${lines[-1]}" == "FAIL: raw size != <size unit=M>2" ]]
    # raw right, but the xz decompresses to something else
    fixture_bundle $n; head -c 1048576 /dev/zero > "$T/raw2"
    truncate -s 2097152 "$T/raw2"; cp "$T/raw2" "$T/b/x.raw"; xz -T0 -f "$T/b/x.raw"; mv "$T/b/x.raw.xz" "$T/b/$n"
    (cd "$T/b" && sha256sum $n > $n.sha256)
    run qdistro_prove_release "$T/raw" "$T/b" $n 1
    [ "$status" -eq 1 ]; [[ "${lines[-1]}" == "FAIL: decompressed size != <size unit=M>1" ]]
    # checksum mismatch
    fixture_bundle $n; sed -i 's/^[0-9a-f]*/0000000000000000000000000000000000000000000000000000000000000000/' "$T/b/$n.sha256"
    run qdistro_prove_release "$T/raw" "$T/b" $n 1
    [ "$status" -eq 1 ]; [[ "${lines[-1]}" == "FAIL: sha256 mismatch" ]]
    # checksum file that names another artifact
    fixture_bundle $n; sed -i "s/ $n\$/ other.raw.xz/" "$T/b/$n.sha256"
    run qdistro_prove_release "$T/raw" "$T/b" $n 1
    [ "$status" -eq 1 ]; [[ "${lines[-1]}" == "FAIL: $n.sha256 names 'other.raw.xz', not $n" ]]
    # a lookalike that a regex compare would accept (dots as any-char)
    fixture_bundle $n; sed -i "s/ $n\$/ qdistro-0.1.0-20260902XrawXxz/" "$T/b/$n.sha256"
    run qdistro_prove_release "$T/raw" "$T/b" $n 1
    [ "$status" -eq 1 ]; [[ "${lines[-1]}" == "FAIL: $n.sha256 names 'qdistro-0.1.0-20260902XrawXxz', not $n" ]]
    # two records
    fixture_bundle $n; (cd "$T/b" && sha256sum $n >> $n.sha256)
    run qdistro_prove_release "$T/raw" "$T/b" $n 1
    [ "$status" -eq 1 ]; [[ "${lines[-1]}" == "FAIL: $n.sha256 has 2 records, want 1" ]]
    # corrupted stream whose checksum file was regenerated: only xz -t sees it
    fixture_bundle $n; printf '\xff\xff\xff\xff' | dd of="$T/b/$n" bs=1 seek=100 conv=notrunc status=none
    (cd "$T/b" && sha256sum $n > $n.sha256)
    run qdistro_prove_release "$T/raw" "$T/b" $n 1
    [ "$status" -eq 1 ]; [[ "${lines[-1]}" == "FAIL: xz -t" ]]
    # wrong name (no such artifact in the bundle)
    fixture_bundle $n
    run qdistro_prove_release "$T/raw" "$T/b" qdistro-0.1.0-19990101.raw.xz 1
    [ "$status" -eq 1 ]; [[ "${lines[-1]}" == FAIL:\ artifact\ missing:* ]]
    # and the caller survives a failure the way build-in-vm.sh invokes it
    run bash -c "set -euo pipefail; source '$IMAGE/lib/release-proof.sh'; ( qdistro_prove_release '$T/raw' '$T/b' $n 2 ) > '$T/out' 2>&1 || echo CALLER-SAW-FAILURE; echo TAIL"
    [ "$status" -eq 0 ]
    [[ "$output" == *CALLER-SAW-FAILURE*TAIL* ]]
    grep -q '^FAIL: raw size' "$T/out"
}

@test "build-in-vm.sh: runs the proof in a subshell and dies with the log on failure" {
    local b="$IMAGE/build-in-vm.sh"
    grep -q '^\. "\$HERE/lib/release-proof.sh"$' "$b"
    grep -q '^( qdistro_prove_release "\$host_raw" "\$HOST_BUILD_DIR/bundle" "\$XZ_NAME" "\$IMAGE_SIZE_MB" )' "$b"
    grep -q 'die "release artifact check failed' "$b"
    # the copy-out is the library's settle loop, and no `ls | head` probe is
    # left to exit the driver under pipefail (round-2 review, runs 24-26)
    grep -q '^qdistro_copy_out_settled "\$BUILD_DISK" "\$HOST_BUILD_DIR"' "$b"
    ! grep -qE '\$\(ls [^)]*\| head' "$b"
    grep -q "^trap 'rc=\$?; case \$- in \*e\*)" "$b"
    grep -q '^set -Eeuo pipefail$' "$b"       # errtrace: the trap covers functions
    grep -q '^\. "\$HERE/lib/copy-out.sh"$' "$b"
}

# A scratch build disk shaped like the builder's (bare xfs on the whole
# device, /out with a raw and a bundle, NO install ISO), so the exact
# guestfish stream the driver sends is exercised. Needs guestfish + a
# libguestfs appliance (~10 s); skipped where absent.
scratch_build_disk() {
    command -v guestfish >/dev/null 2>&1 || skip "guestfish not installed"
    command -v qemu-img  >/dev/null 2>&1 || skip "qemu-img not installed"
    export LIBGUESTFS_BACKEND="${LIBGUESTFS_BACKEND:-direct}"
    qemu-img create -q -f raw "$T/build.img" 300M
    guestfish -a "$T/build.img" <<'GF' >/dev/null 2>&1 || skip "libguestfs appliance unavailable"
run
mkfs xfs /dev/sda
mount /dev/sda /
mkdir /out
mkdir /out/bundle
write /out/qdistro.x86_64-0.1.0.raw "RAW"
write /out/qdistro.x86_64-0.1.0.packages "PKG"
write /out/bundle/qdistro-0.1.0-20260902.raw.xz "XZ"
write /out/bundle/qdistro-0.1.0-20260902.raw.xz.sha256 "SUM"
GF
}

@test "copy-out: a missing install ISO does not abort the stream; raw and bundle/ both land (run 24)" {
    scratch_build_disk
    source "$IMAGE/lib/copy-out.sh"
    mkdir -p "$T/dest"
    run qdistro_copy_out "$T/build.img" "$T/dest"
    [ "$status" -eq 0 ]
    [[ "$output" == *"/out/*.install.iso"* ]]      # the ignored error is still reported
    [ "$(cat "$T/dest/qdistro.x86_64-0.1.0.raw")" = RAW ]
    [ "$(cat "$T/dest/bundle/qdistro-0.1.0-20260902.raw.xz")" = XZ ]
    [ -f "$T/dest/bundle/qdistro-0.1.0-20260902.raw.xz.sha256" ]
    [ "$(cat "$T/dest/qdistro.x86_64-0.1.0.packages")" = PKG ]
    [ ! -e "$T/dest/install.iso" ]
    # the driver keeps its own error handling: the call is `|| true` inside
    # the retry loop, and the size checks decide
    grep -q '^qdistro_copy_out_settled "\$BUILD_DISK" "\$HOST_BUILD_DIR" "\$IN_VM_RAW_SIZE" "\$XZ_NAME" "\$IN_VM_XZ_SIZE" "\$IN_VM_ISO_SIZE" "\$LOGS/copy-out.log"' "$IMAGE/build-in-vm.sh"
    ! grep -q '^glob copy-out /out/\*.install.iso' "$IMAGE/build-in-vm.sh"
}

@test "copy-out: the settle loop accepts only a raw and an xz of the sizes seen in the VM, and retries otherwise" {
    scratch_build_disk
    source "$IMAGE/lib/copy-out.sh"
    mkdir -p "$T/dest"
    # sizes as the in-VM inventory would report them: RAW=3 bytes, XZ=2 bytes
    QDISTRO_COPY_OUT_TRIES=2 QDISTRO_COPY_OUT_SLEEP_S=0 \
        run qdistro_copy_out_settled "$T/build.img" "$T/dest" 3 qdistro-0.1.0-20260902.raw.xz 2 "" "$T/copy.log"
    [ "$status" -eq 0 ]
    [[ "$output" == *"settled"* ]]
    [ -f "$T/dest/bundle/qdistro-0.1.0-20260902.raw.xz.sha256" ]
    # wrong xz size (a stale or truncated copy) never settles
    rm -rf "$T/dest"; mkdir -p "$T/dest"
    QDISTRO_COPY_OUT_TRIES=2 QDISTRO_COPY_OUT_SLEEP_S=0 \
        run qdistro_copy_out_settled "$T/build.img" "$T/dest" 3 qdistro-0.1.0-20260902.raw.xz 999 "" "$T/copy.log"
    [ "$status" -eq 1 ]
    [[ "$output" == *"not settled yet (attempt 1/2"* ]]
    [[ "$output" == *"not settled yet (attempt 2/2"* ]]
    # an ISO size given but no ISO on the disk: never settles either
    QDISTRO_COPY_OUT_TRIES=1 QDISTRO_COPY_OUT_SLEEP_S=0 \
        run qdistro_copy_out_settled "$T/build.img" "$T/dest" 3 qdistro-0.1.0-20260902.raw.xz 2 12345 "$T/copy.log"
    [ "$status" -eq 1 ]
    # the ignored ISO glob line went to the log, not the caller's output
    grep -q "install.iso" "$T/copy.log"
}

@test "copy-out: a missing bundle/ IS a failure (required), and unsafe paths are refused" {
    scratch_build_disk
    guestfish -a "$T/build.img" -m /dev/sda <<'GF' >/dev/null 2>&1
rm-rf /out/bundle
GF
    source "$IMAGE/lib/copy-out.sh"
    mkdir -p "$T/dest" "$T/de st"
    run qdistro_copy_out "$T/build.img" "$T/dest"
    [ "$status" -ne 0 ]
    [ "$(cat "$T/dest/qdistro.x86_64-0.1.0.raw")" = RAW ]   # the raw before it still copied
    [ ! -e "$T/dest/bundle" ]
    run qdistro_copy_out "$T/build.img" "$T/de st"
    [ "$status" -eq 2 ]; [[ "$output" == *"refusing path"* ]]
    run qdistro_copy_out "$T/build.img" "$T/nonexistent"
    [ "$status" -eq 2 ]; [[ "$output" == *"not a directory"* ]]
}

@test "ci image gate: with no install ISO the install stages are recorded as skipped, not run" {
    local g="$REPO/ci/lib/gates/image.sh"
    grep -q 'record_skip image install-test.sh image' "$g"
    grep -q 'installiso=false' "$g"
}
