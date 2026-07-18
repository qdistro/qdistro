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
# Mirrors the exact reuse predicate: reuse the derivative ONLY when it exists
# AND its .provenance stamp equals the current verified cloud digest.
provenance_reuse() {
    local baked="$1" digest="$2"
    [ -s "$baked" ] && [ "$(cat "$baked.provenance" 2>/dev/null)" = "$digest" ]
}

@test "provenance gate: rebuild when the derivative is absent" {
    run provenance_reuse "$WORK/absent.qcow2" "deadbeef"
    [ "$status" -ne 0 ]
}

@test "provenance gate: rebuild when the stamp is missing or mismatched" {
    printf 'img' > "$WORK/baked.qcow2"
    run provenance_reuse "$WORK/baked.qcow2" "deadbeef"   # no .provenance
    [ "$status" -ne 0 ]
    printf 'OLDDIGEST\n' > "$WORK/baked.qcow2.provenance" # stale stamp
    run provenance_reuse "$WORK/baked.qcow2" "deadbeef"
    [ "$status" -ne 0 ]
}

@test "provenance gate: reuse only when the stamp matches the verified digest" {
    printf 'img' > "$WORK/baked.qcow2"
    printf 'deadbeef\n' > "$WORK/baked.qcow2.provenance"
    run provenance_reuse "$WORK/baked.qcow2" "deadbeef"
    [ "$status" -eq 0 ]
}
