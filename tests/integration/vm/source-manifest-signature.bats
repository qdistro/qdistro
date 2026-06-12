#!/usr/bin/env bats
# Detached-signature tests for scripts/install/verify-source-manifest.sh.
#
# No network and no VM required: a throwaway GPG home generates a local
# signing key, exports a gpgv keyring, signs tiny manifests, and proves the
# verifier checks BOTH signature integrity and bootstrap-compatible shape.

setup() {
    SRC_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" \
                    rev-parse --show-toplevel 2>/dev/null)"
    VERIFY="$SRC_ROOT/scripts/install/verify-source-manifest.sh"
    [ -x "$VERIFY" ] || { echo "verifier not found at $VERIFY" >&2; return 1; }

    WORK="$BATS_TEST_TMPDIR/work"
    GNUPGHOME="$WORK/gnupg"
    export GNUPGHOME
    mkdir -p "$GNUPGHOME"
    chmod 0700 "$GNUPGHOME"

    KEY_USER="Qdistro Test Release <release-test@qdistro.invalid>"
    gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
        --quick-generate-key "$KEY_USER" ed25519 sign 0
    gpg --batch --quiet --export "$KEY_USER" > "$WORK/keyring.gpg"
}

write_manifest() {
    local path="$1"
    cat > "$path" <<'EOF'
qdistro  0000000000000000000000000000000000000000
qdwin    1111111111111111111111111111111111111111
EOF
}

sign_manifest() {
    local path="$1"
    gpg --batch --quiet --yes --pinentry-mode loopback --passphrase '' \
        --local-user "$KEY_USER" --detach-sign --output "$path.sig" "$path"
}

@test "verify: signed valid manifest passes" {
    manifest="$WORK/source-manifest.txt"
    write_manifest "$manifest"
    sign_manifest "$manifest"

    run "$VERIFY" "$manifest" "$manifest.sig" "$WORK/keyring.gpg"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    [[ "$output" == *"OK $manifest"* ]]
}

@test "verify: tampered manifest fails signature verification" {
    manifest="$WORK/source-manifest.txt"
    write_manifest "$manifest"
    sign_manifest "$manifest"
    printf 'qdshell 2222222222222222222222222222222222222222\n' >> "$manifest"

    run "$VERIFY" "$manifest" "$manifest.sig" "$WORK/keyring.gpg"
    [ "$status" -ne 0 ] || { echo "expected tamper failure" >&2; return 1; }
}

@test "verify: signed malformed manifest fails lint" {
    manifest="$WORK/source-manifest.txt"
    printf 'qdistro deadbeef\n' > "$manifest"
    sign_manifest "$manifest"

    run "$VERIFY" "$manifest" "$manifest.sig" "$WORK/keyring.gpg"
    [ "$status" -ne 0 ] || { echo "expected lint failure" >&2; return 1; }
    [[ "$output" == *"not a 40-hex"* ]]
}

@test "verify: script is valid bash" {
    run bash -n "$VERIFY"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}
