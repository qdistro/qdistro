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

# --- bootstrap gate: verify_manifest_signature --------------------------
# Source the bootstrap and call its verify_manifest_signature with the manifest
# / detached-sig / keyring pointed via env. Proves the bootstrap runs the
# verifier BEFORE any clone, and is fail-closed once the manifest is populated.
BOOT() { printf '%s' "$SRC_ROOT/scripts/install/qdistro-bootstrap.sh"; }

run_gate() {
    # args: PROFILE  MANIFEST  SIG_OR_-  KEYRING_OR_-  [SIGNER_OR_-]
    local profile="$1" manifest="$2" sig="$3" keyring="$4" signer="${5:-}"
    # Pass the bootstrap path as $1, NOT $0: sourcing it with BASH_SOURCE[0]==$0
    # would trip its `main` guard (require_root). Keep $0 a sentinel ('gate').
    run env \
        QDISTRO_PROFILE="$profile" \
        QDISTRO_SOURCE_MANIFEST="$manifest" \
        ${sig:+QDISTRO_SOURCE_MANIFEST_SIG="$sig"} \
        ${keyring:+QDISTRO_RELEASE_KEYRING="$keyring"} \
        ${signer:+QDISTRO_RELEASE_SIGNER="$signer"} \
        bash -c '. "$1" >/dev/null 2>&1; verify_manifest_signature' gate "$(BOOT)"
}

# Drive the REAL fetch_sources entrypoint (the production call site of the
# gate). With $REPO_ROOT an empty dir and a populated-but-unverifiable manifest,
# the gate must die BEFORE any clone/`install -d`. SKIP arg ('skip'|'') toggles
# --skip-sources so we can prove the gate fires even on the skip path.
run_fetch() {
    # args: SKIP_OR_-  MANIFEST  SIG_OR_-  KEYRING_OR_-  REPO_ROOT
    local skip="$1" manifest="$2" sig="$3" keyring="$4" reporoot="$5"
    run env \
        QDISTRO_PROFILE=release \
        QDISTRO_SOURCE_MANIFEST="$manifest" \
        ${sig:+QDISTRO_SOURCE_MANIFEST_SIG="$sig"} \
        ${keyring:+QDISTRO_RELEASE_KEYRING="$keyring"} \
        QDISTRO_REPO_ROOT="$reporoot" \
        bash -c '
            REPO_ROOT="'"$reporoot"'"
            SKIP_SOURCES="'"$skip"'"
            . "$1" >/dev/null 2>&1
            REPO_ROOT="'"$reporoot"'"; SKIP_SOURCES="'"$skip"'"
            fetch_sources
        ' gate "$(BOOT)"
}

@test "gate: dev profile skips verification (no keyring needed)" {
    manifest="$WORK/source-manifest.txt"
    write_manifest "$manifest"   # populated, but dev never verifies
    run_gate dev "$manifest" "/nope.sig" "/nope.gpg"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}

@test "gate: unsigned stub (no active pins) is a no-op in release profile" {
    manifest="$WORK/source-manifest.txt"
    printf '# all comments, no pins\n#qdistro 0000\n' > "$manifest"
    run_gate release "$manifest" "/nope.sig" "/nope.gpg"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}

@test "gate: populated manifest with NO keyring is FATAL (release)" {
    manifest="$WORK/source-manifest.txt"
    write_manifest "$manifest"
    sign_manifest "$manifest"
    run_gate release "$manifest" "$manifest.sig" "$WORK/missing-keyring.gpg"
    [ "$status" -ne 0 ] || { echo "expected fatal (no keyring)" >&2; return 1; }
    [[ "$output" == *"no release keyring"* ]] || { echo "$output" >&2; return 1; }
}

@test "gate: populated manifest with valid signature passes (release)" {
    manifest="$WORK/source-manifest.txt"
    write_manifest "$manifest"
    sign_manifest "$manifest"
    run_gate release "$manifest" "$manifest.sig" "$WORK/keyring.gpg"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}

@test "gate: tampered populated manifest is FATAL (release)" {
    manifest="$WORK/source-manifest.txt"
    write_manifest "$manifest"
    sign_manifest "$manifest"
    printf 'qdshell 2222222222222222222222222222222222222222\n' >> "$manifest"
    run_gate release "$manifest" "$manifest.sig" "$WORK/keyring.gpg"
    [ "$status" -ne 0 ] || { echo "expected fatal (tamper)" >&2; return 1; }
}

@test "gate: signer check is AUTHORITATIVE against the gpgv signing key" {
    manifest="$WORK/source-manifest.txt"
    # The in-document signer= claims 0xAAAA, but it is the REAL signing key that
    # matters: an expected-signer that the actual key doesn't match is fatal,
    # even though the document is validly signed and self-claims a signer.
    cat > "$manifest" <<EOF
qdistro  0000000000000000000000000000000000000000 signer=0xAAAA
qdwin    1111111111111111111111111111111111111111 signer=0xAAAA
EOF
    sign_manifest "$manifest"
    # A full 40-hex fingerprint that is NOT the real signing key.
    run_gate release "$manifest" "$manifest.sig" "$WORK/keyring.gpg" "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    [ "$status" -ne 0 ] || { echo "expected fatal (wrong signer)" >&2; return 1; }
    [[ "$output" == *"not the expected signer"* ]] || { echo "$output" >&2; return 1; }
}

key_fpr() {
    gpg --batch --with-colons --fingerprint "$KEY_USER" \
        | awk -F: '/^fpr:/{print $10; exit}'
}

@test "gate: signer check passes when EXPECT_SIGNER is the FULL real fingerprint" {
    manifest="$WORK/source-manifest.txt"
    write_manifest "$manifest"
    sign_manifest "$manifest"
    run_gate release "$manifest" "$manifest.sig" "$WORK/keyring.gpg" "0x$(key_fpr)"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}

@test "gate: signer check REJECTS a short key id (not collision-resistant)" {
    manifest="$WORK/source-manifest.txt"
    write_manifest "$manifest"
    sign_manifest "$manifest"
    local fpr; fpr="$(key_fpr)"
    # The real signing key's own last-16 hex must STILL be rejected: we require
    # the full 40-hex fingerprint, so a short id is refused even when it matches.
    run_gate release "$manifest" "$manifest.sig" "$WORK/keyring.gpg" "0x${fpr: -16}"
    [ "$status" -ne 0 ] || { echo "short key id must be rejected" >&2; return 1; }
    [[ "$output" == *"FULL 40-hex fingerprint"* ]] || { echo "$output" >&2; return 1; }
}

# --- production call site: fetch_sources runs the gate before any clone ------
@test "fetch_sources: gate dies BEFORE any clone for a populated unsigned manifest" {
    manifest="$WORK/source-manifest.txt"
    write_manifest "$manifest"            # populated, no .sig, no keyring
    local rr="$WORK/srcroot"; mkdir -p "$rr"
    run_fetch "" "$manifest" "" "$WORK/missing-keyring.gpg" "$rr"
    [ "$status" -ne 0 ] || { echo "fetch_sources should die on unsigned pinned manifest" >&2; return 1; }
    [[ "$output" == *"no release keyring"* ]] || { echo "$output" >&2; return 1; }
    # Nothing was cloned: $REPO_ROOT stays empty (die preceded `install -d`/clone).
    [ -z "$(ls -A "$rr")" ] || { echo "clone happened before the gate!"; ls -A "$rr" >&2; return 1; }
}

@test "fetch_sources: --skip-sources STILL runs the gate (no bypass)" {
    manifest="$WORK/source-manifest.txt"
    write_manifest "$manifest"
    local rr="$WORK/srcroot2"; mkdir -p "$rr"
    run_fetch "skip" "$manifest" "" "$WORK/missing-keyring.gpg" "$rr"
    [ "$status" -ne 0 ] || { echo "--skip-sources must not bypass the signature gate" >&2; return 1; }
    [[ "$output" == *"no release keyring"* ]] || { echo "$output" >&2; return 1; }
}

@test "fetch_sources: signed manifest gate passes, then proceeds (no required source -> dies later, not at gate)" {
    manifest="$WORK/source-manifest.txt"
    write_manifest "$manifest"
    sign_manifest "$manifest"
    local rr="$WORK/srcroot3"; mkdir -p "$rr"
    run_fetch "skip" "$manifest" "$manifest.sig" "$WORK/keyring.gpg" "$rr"
    # --skip-sources + signed manifest + no present repos -> gate passes, the
    # per-repo verify loop is a no-op (nothing present), fetch_sources returns 0.
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}

# --- manifest_has_pins: a populated-but-malformed line still trips the gate --
@test "gate: a populated-but-malformed line (bad SHA) still requires a signature" {
    manifest="$WORK/source-manifest.txt"
    # Active line, but the SHA is uppercase/invalid: must NOT be treated as
    # 'no pins' (which would skip the gate and reach a clone unsigned).
    printf 'qdistro AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n' > "$manifest"
    run_gate release "$manifest" "/nope.sig" "/nope.gpg"
    [ "$status" -ne 0 ] || { echo "malformed populated manifest must trip the gate" >&2; return 1; }
    [[ "$output" == *"no release keyring"* || "$output" == *"no detached signature"* ]] \
        || { echo "$output" >&2; return 1; }
}

# --- skip-sources still PIN-verifies pre-staged checkouts (load-bearing) -----
@test "fetch_sources: --skip-sources pin-verifies a PRESENT checkout (fails on wrong commit)" {
    # A present, signed-manifest install with a pre-staged repo whose HEAD is NOT
    # the pinned commit must die in the skip-sources pin loop, before any build.
    export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
    export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
    local rr="$WORK/staged"; mkdir -p "$rr/qdistro/daemons"
    git -C "$rr/qdistro" init -q
    printf 'x\n' > "$rr/qdistro/daemons/keep"
    git -C "$rr/qdistro" add -A && git -C "$rr/qdistro" commit -q -m init
    # Manifest pins a DIFFERENT (nonexistent) commit for qdistro; sign it.
    manifest="$WORK/source-manifest.txt"
    printf 'qdistro aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n' > "$manifest"
    sign_manifest "$manifest"
    run_fetch "skip" "$manifest" "$manifest.sig" "$WORK/keyring.gpg" "$rr"
    [ "$status" -ne 0 ] || { echo "skip-sources must pin-verify the present checkout" >&2; return 1; }
    [[ "$output" == *"pinned commit"* || "$output" == *"!= pinned"* ]] || { echo "$output" >&2; return 1; }
}

# --- TOCTOU: later reads come from the verified copy, not the original -------
@test "gate: after verification, manifest_pin reads the VERIFIED copy (TOCTOU)" {
    manifest="$WORK/source-manifest.txt"
    write_manifest "$manifest"            # qdistro 0000...0
    sign_manifest "$manifest"
    # Source bootstrap, verify, then MUTATE the original path, then read the pin:
    # it must reflect the signed copy (0000...0), not the post-verify mutation.
    run env QDISTRO_PROFILE=release \
        QDISTRO_SOURCE_MANIFEST="$manifest" \
        QDISTRO_SOURCE_MANIFEST_SIG="$manifest.sig" \
        QDISTRO_RELEASE_KEYRING="$WORK/keyring.gpg" \
        bash -c '
            . "$1" >/dev/null 2>&1
            verify_manifest_signature >/dev/null 2>&1 || { echo GATE_FAILED; exit 7; }
            printf "qdistro %040d\n" 9 > "'"$manifest"'"   # tamper the ORIGINAL
            manifest_pin qdistro
        ' gate "$(BOOT)"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    [ "$output" = "0000000000000000000000000000000000000000" ] \
        || { echo "read post-verify mutation instead of the verified copy: $output" >&2; return 1; }
}
