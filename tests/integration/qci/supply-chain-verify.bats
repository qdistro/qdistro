#!/usr/bin/env bats
#
# Host-only tests for the J25 supply-chain hardening:
#   - the openSUSE cloud-image verification helper
#     (scripts/vm/lib/opensuse-cloud-image.sh) fails closed on a bad
#     signature / wrong signing key / digest mismatch, and
#   - the zypper --no-gpg-checks profile gate is closed by default
#     (only the `dev` profile skips GPG checks).
# These pin the fail-closed posture so a regression is caught host-side,
# without needing a real image build or network.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    LIB="$REPO_ROOT/scripts/vm/lib/opensuse-cloud-image.sh"
    KEY="$REPO_ROOT/scripts/vm/keys/opensuse-tumbleweed-signing-key.asc"
    PROFILE_LIB="$REPO_ROOT/scripts/install/lib/qdistro-profile.sh"
    FIXT="$BATS_TEST_DIRNAME/fixtures"
    WORK="$BATS_TEST_TMPDIR/work"
    mkdir -p "$WORK"
    TODAY="$(date -u +%Y%m%d)"
    YESTERDAY="$(date -u -d yesterday +%Y%m%d)"
}

# The pinned fingerprint the helper trusts.
FPR="AD485664E901B867051AB15F35A2F86E29B700A4"

# Build a *self-generated* key + signed checksum so the crypto path is
# exercised offline. The helper is pointed at this throwaway key via
# OPENSUSE_TW_KEY/OPENSUSE_TW_FPR overrides; the REAL checked-in key's
# fingerprint is asserted separately below.
make_local_signed_fixture() {
    export GNUPGHOME="$WORK/gnupg"; mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"
    gpg --batch --quiet --passphrase '' --pinentry-mode loopback \
        --quick-generate-key "qdistro test signer" default default never >/dev/null 2>&1
    LOCAL_FPR="$(gpg --batch --with-colons --fingerprint --list-keys \
        | awk -F: '$1=="fpr"{print $10; exit}')"
    gpg --batch --quiet --armor --export "$LOCAL_FPR" > "$WORK/local-key.asc"
    printf 'x' > "$WORK/image.qcow2"
    ( cd "$WORK" && sha256sum image.qcow2 > image.qcow2.sha256 )
    gpg --batch --quiet --passphrase '' --pinentry-mode loopback \
        --detach-sign --armor -o "$WORK/image.qcow2.sha256.asc" "$WORK/image.qcow2.sha256"
}

@test "checked-in openSUSE key carries the pinned fingerprint" {
    export GNUPGHOME="$WORK/g2"; mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"
    got="$(gpg --batch --quiet --show-keys --with-colons "$KEY" \
        | awk -F: '$1=="fpr"{print $10; exit}')"
    [ "$got" = "$FPR" ]
}

@test "the REAL checked-in key validates a REAL openSUSE signed checksum (key-rotation / format guard)" {
    # Uses the default (checked-in) key + pinned fingerprint — no overrides.
    # If openSUSE rotates to a subkey-signing model or changes the .sha256
    # format, or the checked-in key/fingerprint drift, this fails host-side.
    bash -c ". '$LIB'; verify_opensuse_sha256_signature '$FIXT/opensuse-cloud.sha256' '$FIXT/opensuse-cloud.sha256.asc'"
}

@test "helper accepts a correctly-signed checksum + matching image (name-bound)" {
    make_local_signed_fixture
    OPENSUSE_TW_KEY="$WORK/local-key.asc" OPENSUSE_TW_FPR="$LOCAL_FPR" \
        bash -c ". '$LIB'; verify_cached_cloud_image '$WORK/image.qcow2' '$WORK/image.qcow2.sha256' '$WORK/image.qcow2.sha256.asc' 'image.qcow2'"
}

@test "helper rejects a signed checksum that is for a DIFFERENT artifact name" {
    make_local_signed_fixture
    # Signature is valid over the .sha256, but the digest line names
    # image.qcow2, not the artifact we asked to verify => reject (replay guard).
    run env OPENSUSE_TW_KEY="$WORK/local-key.asc" OPENSUSE_TW_FPR="$LOCAL_FPR" \
        bash -c ". '$LIB'; verify_cached_cloud_image '$WORK/image.qcow2' '$WORK/image.qcow2.sha256' '$WORK/image.qcow2.sha256.asc' 'some-other-image.qcow2'"
    [ "$status" -ne 0 ]
}

@test "helper rejects a tampered checksum file (bad signature)" {
    make_local_signed_fixture
    printf '%064d  image.qcow2\n' 0 > "$WORK/image.qcow2.sha256"   # rewrite after signing
    run env OPENSUSE_TW_KEY="$WORK/local-key.asc" OPENSUSE_TW_FPR="$LOCAL_FPR" \
        bash -c ". '$LIB'; verify_opensuse_sha256_signature '$WORK/image.qcow2.sha256' '$WORK/image.qcow2.sha256.asc'"
    [ "$status" -ne 0 ]
}

@test "helper rejects a valid signature from an unexpected key (fingerprint pin)" {
    make_local_signed_fixture
    # Correct key file, but pin a different fingerprint => must reject.
    run env OPENSUSE_TW_KEY="$WORK/local-key.asc" OPENSUSE_TW_FPR="$FPR" \
        bash -c ". '$LIB'; verify_opensuse_sha256_signature '$WORK/image.qcow2.sha256' '$WORK/image.qcow2.sha256.asc'"
    [ "$status" -ne 0 ]
}

@test "helper rejects an image whose digest does not match the signed checksum" {
    make_local_signed_fixture
    printf 'DIFFERENT CONTENT' > "$WORK/image.qcow2"   # digest no longer matches
    run env OPENSUSE_TW_KEY="$WORK/local-key.asc" OPENSUSE_TW_FPR="$LOCAL_FPR" \
        bash -c ". '$LIB'; verify_cached_cloud_image '$WORK/image.qcow2' '$WORK/image.qcow2.sha256' '$WORK/image.qcow2.sha256.asc' 'image.qcow2'"
    [ "$status" -ne 0 ]
}

@test "cloud cache accepts signed bytes only when they match the test substrate pin" {
    make_local_signed_fixture
    digest="$(sha256sum "$WORK/image.qcow2" | awk '{print $1}')"
    run env OPENSUSE_TW_KEY="$WORK/local-key.asc" OPENSUSE_TW_FPR="$LOCAL_FPR" \
        bash -c ". '$LIB'; download_verified_cloud_image 'https://invalid.example/image.qcow2' '$WORK/image.qcow2' '$digest'"
    [ "$status" -eq 0 ]
    run env OPENSUSE_TW_KEY="$WORK/local-key.asc" OPENSUSE_TW_FPR="$LOCAL_FPR" \
        bash -c ". '$LIB'; download_verified_cloud_image 'https://invalid.example/image.qcow2' '$WORK/image.qcow2' '0000000000000000000000000000000000000000000000000000000000000000'"
    [ "$status" -ne 0 ]
    [[ "$output" == *"differs from test substrate pin"* ]]
}

@test "cloud test substrate stamp rejects old snapshot and modified disk" {
    local substrate="$REPO_ROOT/scripts/vm/lib/test-substrate.sh" manifest="$WORK/substrate.conf"
    printf 'schema=1\narch=%s\ncloud_url=https://invalid.example/cloud.qcow2\ncloud_sha256=%064d\nsnapshot=%s\n' \
        "$(uname -m)" 1 "$TODAY" > "$manifest"
    printf 'base' > "$WORK/base.qcow2"
    run env QDISTRO_TEST_SUBSTRATE="$manifest" bash -c \
        ". '$substrate'; qdistro_load_test_substrate; qdistro_substrate_write_stamp '$WORK/base.qcow2' admin \"\$QDISTRO_SUBSTRATE_CLOUD_SHA256\"; qdistro_substrate_stamp_ok '$WORK/base.qcow2' admin \"\$QDISTRO_SUBSTRATE_CLOUD_SHA256\""
    [ "$status" -eq 0 ]
    sed -i "s/snapshot=$TODAY/snapshot=$YESTERDAY/" "$manifest"
    run env QDISTRO_TEST_SUBSTRATE="$manifest" bash -c \
        ". '$substrate'; qdistro_load_test_substrate; qdistro_substrate_stamp_ok '$WORK/base.qcow2' admin \"\$QDISTRO_SUBSTRATE_CLOUD_SHA256\""
    [ "$status" -ne 0 ]
    sed -i "s/snapshot=$YESTERDAY/snapshot=$TODAY/" "$manifest"
    printf 'tamper' >> "$WORK/base.qcow2"
    run env QDISTRO_TEST_SUBSTRATE="$manifest" bash -c \
        ". '$substrate'; qdistro_load_test_substrate; qdistro_substrate_stamp_ok '$WORK/base.qcow2' admin \"\$QDISTRO_SUBSTRATE_CLOUD_SHA256\""
    [ "$status" -ne 0 ]
}

@test "cloud test substrate uses distinct paths for snapshot and recipe changes" {
    local substrate="$REPO_ROOT/scripts/vm/lib/test-substrate.sh" manifest="$WORK/substrate.conf" path1 path2
    printf 'schema=1\narch=%s\ncloud_url=https://invalid.example/cloud.qcow2\ncloud_sha256=%064d\nsnapshot=%s\n' \
        "$(uname -m)" 1 "$TODAY" > "$manifest"
    path1="$(QDISTRO_TEST_SUBSTRATE="$manifest" bash -c ". '$substrate'; qdistro_load_test_substrate; qdistro_substrate_base_path baked")"
    sed -i "s/snapshot=$TODAY/snapshot=$YESTERDAY/" "$manifest"
    path2="$(QDISTRO_TEST_SUBSTRATE="$manifest" bash -c ". '$substrate'; qdistro_load_test_substrate; qdistro_substrate_base_path baked")"
    [ "$path1" != "$path2" ]
    [[ "$path1" == *"$TODAY"* ]]
    [[ "$path2" == *"$YESTERDAY"* ]]
}

@test "baked tier-5 omission has a distinct path and cannot reuse the default stamp" {
    local substrate="$REPO_ROOT/scripts/vm/lib/test-substrate.sh" manifest="$WORK/substrate.conf" default_path skip_path
    printf 'schema=1\narch=%s\ncloud_url=https://invalid.example/cloud.qcow2\ncloud_sha256=%064d\nsnapshot=%s\n' \
        "$(uname -m)" 1 "$TODAY" > "$manifest"
    default_path="$(QDWIN_SKIP_TIER5_BAKE=0 QDISTRO_TEST_SUBSTRATE="$manifest" bash -c ". '$substrate'; qdistro_load_test_substrate; qdistro_substrate_base_path baked")"
    skip_path="$(QDWIN_SKIP_TIER5_BAKE=1 QDISTRO_TEST_SUBSTRATE="$manifest" bash -c ". '$substrate'; qdistro_load_test_substrate; qdistro_substrate_base_path baked")"
    [ "$default_path" != "$skip_path" ]
    printf 'baked' > "$WORK/baked.qcow2"
    run env QDWIN_SKIP_TIER5_BAKE=0 QDISTRO_TEST_SUBSTRATE="$manifest" bash -c \
        ". '$substrate'; qdistro_load_test_substrate; qdistro_substrate_write_stamp '$WORK/baked.qcow2' baked source; qdistro_substrate_stamp_ok '$WORK/baked.qcow2' baked source"
    [ "$status" -eq 0 ]
    run env QDWIN_SKIP_TIER5_BAKE=1 QDISTRO_TEST_SUBSTRATE="$manifest" bash -c \
        ". '$substrate'; qdistro_load_test_substrate; qdistro_substrate_stamp_ok '$WORK/baked.qcow2' baked source"
    [ "$status" -ne 0 ]
}

@test "snapshot freshness accepts day 14 and rejects day 15, future and invalid dates" {
    local substrate="$REPO_ROOT/scripts/vm/lib/test-substrate.sh"
    run bash -c ". '$substrate'; qdistro_substrate_snapshot_fresh 20260914 20260928"
    [ "$status" -eq 0 ]
    run bash -c ". '$substrate'; qdistro_substrate_snapshot_fresh 20260913 20260928"
    [ "$status" -ne 0 ]
    run bash -c ". '$substrate'; qdistro_substrate_snapshot_fresh 20260929 20260928"
    [ "$status" -ne 0 ]
    run bash -c ". '$substrate'; qdistro_substrate_snapshot_fresh 20260230 20260301"
    [ "$status" -ne 0 ]
}

@test "expired manifest is refused before a base path can be selected" {
    local substrate="$REPO_ROOT/scripts/vm/lib/test-substrate.sh" manifest="$WORK/substrate.conf" expired
    expired="$(date -u -d '15 days ago' +%Y%m%d)"
    printf 'schema=1\narch=%s\ncloud_url=https://invalid.example/cloud.qcow2\ncloud_sha256=%064d\nsnapshot=%s\n' \
        "$(uname -m)" 1 "$expired" > "$manifest"
    run env QDISTRO_TEST_SUBSTRATE="$manifest" bash -c \
        ". '$substrate'; qdistro_load_test_substrate && qdistro_substrate_base_path admin"
    [ "$status" -ne 0 ]
    [[ "$output" == *"older than 14 days"* ]]
}

@test "qci refuses an expired cloud pin before starting VM tests" {
    local manifest="$WORK/substrate.conf" expired runs="$WORK/runs"
    expired="$(date -u -d '15 days ago' +%Y%m%d)"
    printf 'schema=1\narch=%s\ncloud_url=https://invalid.example/cloud.qcow2\ncloud_sha256=%064d\nsnapshot=%s\n' \
        "$(uname -m)" 1 "$expired" > "$manifest"
    run env QDISTRO_TEST_SUBSTRATE="$manifest" QCI_RUNS_DIR="$runs" \
        "$REPO_ROOT/ci/bin/qci" vm-smoke
    [ "$status" -eq 10 ]
    local results
    results="$(find "$runs" -name results.tsv -print -quit)"
    [ -s "$results" ]
    grep -Fq 'cloud test substrate freshness' "$results"
    ! awk -F '\t' '$1 == "vm-smoke" || $1 == "bats" || $1 == "gui" { found=1 } END { exit !found }' "$results"
}

@test "VM base default stays cloud-derived while Kiwi remains explicit" {
    local selector="$REPO_ROOT/scripts/vm/lib/vm-base.sh"
    run bash -c ". '$selector'; qdistro_kiwi_base_ok() { return 0; }; qdistro_vm_base_kind"
    [ "$status" -eq 0 ]
    [ "$output" = baked ]
    run env QDISTRO_VM_BASE=auto bash -c ". '$selector'; qdistro_kiwi_base_ok() { return 0; }; qdistro_vm_base_kind"
    [ "$status" -eq 0 ]
    [ "$output" = kiwi ]
}

@test "cloud substrate replaces rolling repositories with signed snapshot repositories" {
    local substrate="$REPO_ROOT/scripts/vm/lib/test-substrate.sh" root="$WORK/root" command
    mkdir -p "$root/etc/zypp/repos.d" "$root/etc/zypp/services.d"
    printf 'baseurl=https://download.opensuse.org/tumbleweed/repo/oss/\n' > "$root/etc/zypp/repos.d/rolling.repo"
    printf 'service\n' > "$root/etc/zypp/services.d/rolling.service"
    command="$(bash -c ". '$substrate'; qdistro_load_test_substrate; qdistro_substrate_repo_command")"
    command="${command//\/etc\//$root\/etc\/}"
    bash -c "$command"
    [ ! -e "$root/etc/zypp/repos.d/rolling.repo" ]
    [ ! -e "$root/etc/zypp/services.d/rolling.service" ]
    [ "$(find "$root/etc/zypp/repos.d" -name '*.repo' | wc -l)" -eq 2 ]
    grep -Fq "history/$(sed -n 's/^snapshot=//p' "$REPO_ROOT/snapshot.conf")/tumbleweed/repo/oss/" "$root/etc/zypp/repos.d/qdistro-snapshot-oss.repo"
    grep -Fq 'gpgcheck=1' "$root/etc/zypp/repos.d/qdistro-snapshot-oss.repo"
    grep -Fq 'keeppackages=1' "$root/etc/zypp/repos.d/qdistro-snapshot-oss.repo"
}

# --- zypper --no-gpg-checks profile gate ----------------------------------

# Echo the gpg_flags array the install-deps gate computes for a given profile.
gate_flags_for() {
    QDISTRO_PROFILE="$1" bash -c "
        . '$PROFILE_LIB'; resolve_profile
        gpg_flags=()
        if is_dev; then gpg_flags=( --no-gpg-checks ); fi
        printf '%s' \"\${gpg_flags[*]}\"
    "
}

@test "hardened profiles (default/daily-driver/release) do NOT skip gpg checks" {
    [ -z "$(gate_flags_for daily-driver)" ]
    [ -z "$(gate_flags_for release)" ]
    # Unset profile defaults to the hardened path.
    [ -z "$(env -u QDISTRO_PROFILE bash -c ". '$PROFILE_LIB'; resolve_profile; is_dev && echo dev || echo hardened")" ] || true
    [ "$(env -u QDISTRO_PROFILE bash -c ". '$PROFILE_LIB'; resolve_profile; is_dev && echo dev || echo hardened")" = "hardened" ]
}

@test "only the dev profile skips gpg checks" {
    [ "$(gate_flags_for dev)" = "--no-gpg-checks" ]
}

# --- tier-5 customized-base provenance gate (build-baked-baseweed.sh) -------
# Mirrors the digest portion of the reuse predicate: reuse the derivative ONLY
# when its provenance binds both the authenticated source and derivative bytes.
# The production predicate additionally requires `qemu-img check`.
provenance_reuse() {
    local baked="$1" digest="$2"
    local source_digest image_digest actual_digest
    [ -s "$baked" ] || return 1
    source_digest="$(awk -F= '$1 == "source_sha256" { print $2; exit }' "$baked.provenance" 2>/dev/null)"
    image_digest="$(awk -F= '$1 == "image_sha256" { print $2; exit }' "$baked.provenance" 2>/dev/null)"
    actual_digest="$(sha256sum "$baked" | awk '{print $1}')"
    [ "$source_digest" = "$digest" ] && [ -n "$image_digest" ] && [ "$image_digest" = "$actual_digest" ]
}

@test "provenance gate: rebuild when the derivative is absent" {
    run provenance_reuse "$WORK/absent.qcow2" "deadbeef"
    [ "$status" -ne 0 ]
}

@test "provenance gate: rebuild when the stamp is missing or mismatched" {
    printf 'img' > "$WORK/baked.qcow2"
    run provenance_reuse "$WORK/baked.qcow2" "deadbeef"   # no .provenance
    [ "$status" -ne 0 ]
    printf 'source_sha256=OLDDIGEST\nimage_sha256=ignored\n' > "$WORK/baked.qcow2.provenance"
    run provenance_reuse "$WORK/baked.qcow2" "deadbeef"
    [ "$status" -ne 0 ]
}

@test "provenance gate: reuse only when source and derivative digests match" {
    printf 'img' > "$WORK/baked.qcow2"
    image_digest="$(sha256sum "$WORK/baked.qcow2" | awk '{print $1}')"
    printf 'source_sha256=deadbeef\nimage_sha256=%s\n' "$image_digest" \
        > "$WORK/baked.qcow2.provenance"
    run provenance_reuse "$WORK/baked.qcow2" "deadbeef"
    [ "$status" -eq 0 ]
}

@test "provenance gate: rejects tampered derivative with retained stamp" {
    printf 'img' > "$WORK/baked.qcow2"
    image_digest="$(sha256sum "$WORK/baked.qcow2" | awk '{print $1}')"
    printf 'source_sha256=deadbeef\nimage_sha256=%s\n' "$image_digest" \
        > "$WORK/baked.qcow2.provenance"
    printf 'tampered' >> "$WORK/baked.qcow2"
    run provenance_reuse "$WORK/baked.qcow2" "deadbeef"
    [ "$status" -ne 0 ]
}

# Every rootless Podman base that builds test payloads comes from the one
# snapshot.conf pin: the native (test binary) builder and the tier-2 workload
# cache pull tumbleweed:<snapshot>, never the rolling :latest. The real scripts
# run against a recording podman that stops them at the pull.
@test "podman builders pull the snapshot.conf-pinned Tumbleweed base, not :latest" {
    local snap fake="$WORK/bin" calls="$WORK/podman-calls" script
    snap="$(sed -n 's/^snapshot=//p' "$REPO_ROOT/snapshot.conf")"
    mkdir -p "$fake"
    cat > "$fake/podman" <<SH
#!/bin/sh
printf '%s\n' "\$*" >> "$calls"
case "\$1" in
    info) echo true ;;
    image) exit 1 ;;
    *) exit 3 ;;
esac
SH
    chmod +x "$fake/podman"
    for script in build-native-podman.sh build-tier2-podman-cache.sh; do
        : > "$calls"
        run env -u QCI_PODMAN_IMAGE PATH="$fake:$PATH" QCI_OFFLINE=0 \
            bash "$REPO_ROOT/scripts/vm/$script"
        [ "$status" -ne 0 ]
        grep -qx "pull registry.opensuse.org/opensuse/tumbleweed:$snap" "$calls"
        run grep -c ':latest' "$calls"
        [ "$output" = 0 ]
    done
    # the tier-2 Containerfiles take the same pin as their FROM tag
    for script in "$REPO_ROOT"/tier2/Containerfile.*; do
        grep -qx 'FROM registry.opensuse.org/opensuse/tumbleweed:${SNAPSHOT}' "$script"
    done
    grep -q -- '--build-arg "SNAPSHOT=$QDISTRO_SUBSTRATE_SNAPSHOT"' "$REPO_ROOT/scripts/vm/build-tier2-podman-cache.sh"
    grep -q -- '--build-arg "SNAPSHOT=$tier2_snapshot"' "$REPO_ROOT/tier2/make-tier2-image.sh"
}
