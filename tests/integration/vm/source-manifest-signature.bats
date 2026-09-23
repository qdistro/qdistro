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
qdistro  0000000000000000000000000000000000000000 tag=v1.0.0
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
    sed -i 's/^qdistro .*/qdistro  2222222222222222222222222222222222222222 tag=v1.0.0/' "$manifest"

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
    sed -i 's/^qdistro .*/qdistro  2222222222222222222222222222222222222222 tag=v1.0.0/' "$manifest"
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

@test "fetch_sources: signed manifest gate passes, then proceeds (empty source root -> dies at the pin, not at the gate)" {
    manifest="$WORK/source-manifest.txt"
    write_manifest "$manifest"
    sign_manifest "$manifest"
    local rr="$WORK/srcroot3"; mkdir -p "$rr"
    run_fetch "skip" "$manifest" "$manifest.sig" "$WORK/keyring.gpg" "$rr"
    # --skip-sources + signed manifest: the signature gate PASSES, then the one
    # monorepo source root is pin-verified. Before the monorepo migration an
    # empty root meant "no sibling present" and was a no-op; now the root IS
    # the (single) pinned source, so an empty one is refused at the pin.
    [ "$status" -ne 0 ] || { echo "an unverifiable empty source root must be refused" >&2; return 1; }
    [[ "$output" != *"no release keyring"* && "$output" != *"signature"*"FAIL"* ]] \
        || { echo "died at the signature gate, not the pin: $output" >&2; return 1; }
    [[ "$output" == *"needs a git checkout"* ]] || { echo "$output" >&2; return 1; }
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
    # Monorepo: the staged checkout IS the source root.
    local rr="$WORK/staged"; mkdir -p "$rr/daemons"
    git -C "$rr" init -q
    printf 'x\n' > "$rr/daemons/keep"
    git -C "$rr" add -A && git -C "$rr" commit -q -m init
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

# =========================================================================
# The whole hardened single-pin chain with a POPULATED, SIGNED fixture:
#   signed manifest -> gate -> clone (or pre-staged checkout) -> trust gate
#   -> pin / checkout / status / tag -> installer paths.
# The tests above prove each link separately. These drive the real
# fetch_sources (release profile) end to end against a local "origin"
# monorepo served through QDISTRO_REPO_URL, as a non-root user (the trust
# gate accepts our own euid in place of root). No network, no VM.
#
# Not covered here (needs root or a VM): a tree owned by ANOTHER uid, a
# real root-owned /opt/qdistro-src, the build/meson/pip steps themselves,
# and the chain installers actually running.
# =========================================================================

# chain_fixture — create $ORIGIN, a monorepo with the dirs fetch_sources and
# the pip step look at. $PIN is a tagged (v1.0.0) commit; $NEXT is a later
# commit on the same branch, so "origin tip" != "pin" and a successful run
# proves the bootstrap detached to the pin rather than taking the tip.
chain_fixture() {
    umask 022
    export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
    export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
    ORIGIN="$WORK/origin"
    mkdir -p "$ORIGIN"/{daemons,qdwin,qdshell,qdterm,qdfileman,scripts/install}
    git -C "$ORIGIN" init -q -b main
    printf 'qdistro\n'          > "$ORIGIN/README"
    printf 'x\n'                > "$ORIGIN/daemons/keep"
    printf "project('qdwin')\n" > "$ORIGIN/qdwin/meson.build"
    printf 'qml\n'              > "$ORIGIN/qdshell/shell.qml"
    printf '[project]\nname = "qterminator"\n' > "$ORIGIN/qdterm/pyproject.toml"
    printf '[project]\nname = "qfileman"\n'    > "$ORIGIN/qdfileman/pyproject.toml"
    printf '#!/bin/sh\n'        > "$ORIGIN/scripts/install/install-sdk-for-vm.sh"
    git -C "$ORIGIN" add -A && git -C "$ORIGIN" commit -q -m release
    PIN=$(git -C "$ORIGIN" rev-parse HEAD)
    git -C "$ORIGIN" tag v1.0.0
    printf 'later\n' > "$ORIGIN/daemons/later"
    git -C "$ORIGIN" add -A && git -C "$ORIGIN" commit -q -m later
    NEXT=$(git -C "$ORIGIN" rev-parse HEAD)
    MANIFEST="$WORK/source-manifest.txt"
}

# signed_manifest <content> — write the manifest and sign it.
signed_manifest() {
    printf '%s\n' "$1" > "$MANIFEST"
    sign_manifest "$MANIFEST"
}

# stage_checkout <dir> [<commit>] — a pre-staged full clone (--skip-sources).
stage_checkout() {
    git clone -q "$ORIGIN" "$1"
    git -C "$1" checkout -q --detach "${2:-$PIN}"
}

# run_chain <skip|""> <repo-root> [<signer>] — the real fetch_sources in the
# release profile with the fixture's signed manifest, sig and keyring. On
# success prints CHAIN_OK and the resulting HEAD. $CHAIN_PATH (optional)
# replaces PATH (used to shim git).
run_chain() {
    local skip="$1" rr="$2" signer="${3:-}"
    run env PATH="${CHAIN_PATH:-$PATH}" \
        QDISTRO_PROFILE=release \
        QDISTRO_SOURCE_MANIFEST="$MANIFEST" \
        QDISTRO_SOURCE_MANIFEST_SIG="$MANIFEST.sig" \
        QDISTRO_RELEASE_KEYRING="$WORK/keyring.gpg" \
        ${signer:+QDISTRO_RELEASE_SIGNER="$signer"} \
        QDISTRO_REPO_URL="$ORIGIN" \
        QDISTRO_REPO_ROOT="$rr" \
        bash -c '
            . "$1" >/dev/null 2>&1
            REPO_ROOT="$2"; SKIP_SOURCES="$3"
            fetch_sources
            echo "CHAIN_OK HEAD=$(git -C "$REPO_ROOT" rev-parse HEAD)"
        ' chain "$(BOOT)" "$rr" "$skip"
}

chain_ok_at_pin() {
    [ "$status" -eq 0 ] || { echo "status=$status: $output" >&2; return 1; }
    [[ "$output" == *"CHAIN_OK HEAD=$PIN"* ]] || { echo "not at the pin: $output" >&2; return 1; }
}

# --- success paths -------------------------------------------------------
@test "chain: fresh acquisition clones the ONE repo and detaches to the signed pin (not the tip)" {
    chain_fixture
    signed_manifest "qdistro $PIN tag=v1.0.0"
    local rr="$WORK/opt-src"                       # absent: fetch_sources creates it
    run_chain "" "$rr" "0x$(key_fpr)"
    chain_ok_at_pin
    [ "$PIN" != "$NEXT" ]
    [[ "$output" == *"source manifest signature OK"* ]] || { echo "$output" >&2; return 1; }
    [[ "$output" == *"qdistro: verified at pinned commit $PIN (tag v1.0.0)"* ]] || { echo "$output" >&2; return 1; }
    [ -d "$rr/.git" ] && [ -f "$rr/qdwin/meson.build" ]
    # One clone at the root: no per-component checkouts, no nested qdistro/.
    [ ! -e "$rr/qdistro" ] && [ -z "$(find "$rr" -mindepth 2 -name .git)" ]
}

@test "chain: --skip-sources with a clean pre-staged checkout at the pin passes" {
    chain_fixture
    signed_manifest "qdistro $PIN tag=v1.0.0"
    local rr="$WORK/staged"; stage_checkout "$rr"
    run_chain skip "$rr" "0x$(key_fpr)"
    chain_ok_at_pin
    [[ "$output" == *"skipping source acquisition"* ]] || { echo "$output" >&2; return 1; }
}

# --- signature failures stop BEFORE any clone ----------------------------
@test "chain: missing detached signature is fatal before any clone" {
    chain_fixture
    printf 'qdistro %s\n' "$PIN" > "$MANIFEST"    # populated, never signed
    local rr="$WORK/opt-src"
    run_chain "" "$rr"
    [ "$status" -ne 0 ]
    [[ "$output" == *"no detached signature"* ]] || { echo "$output" >&2; return 1; }
    [ ! -e "$rr" ] || { echo "source root touched before the gate" >&2; return 1; }
}

@test "chain: invalid signature (manifest edited after signing) is fatal before any clone" {
    chain_fixture
    signed_manifest "qdistro $PIN"
    printf 'qdistro %s\n' "$NEXT" > "$MANIFEST"   # repoint the pin; sig no longer matches
    local rr="$WORK/opt-src"
    run_chain "" "$rr"
    [ "$status" -ne 0 ]
    [[ "$output" == *"verification FAILED"* ]] || { echo "$output" >&2; return 1; }
    [ ! -e "$rr" ]
}

@test "chain: signer mismatch is fatal before any clone" {
    chain_fixture
    signed_manifest "qdistro $PIN"
    local rr="$WORK/opt-src"
    run_chain "" "$rr" "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    [ "$status" -ne 0 ]
    [[ "$output" == *"not the expected signer"* ]] || { echo "$output" >&2; return 1; }
    [ ! -e "$rr" ]
}

@test "chain: a SIGNED legacy per-component pin is refused by the verifier's lint" {
    chain_fixture
    signed_manifest "qdistro $PIN
qdwin $PIN"
    local rr="$WORK/opt-src"
    run_chain "" "$rr" "0x$(key_fpr)"
    [ "$status" -ne 0 ]
    [[ "$output" == *"verification FAILED"* ]] || { echo "$output" >&2; return 1; }
    [ ! -e "$rr" ]
}

@test "chain: a SIGNED duplicate qdistro pin is refused by the verifier's lint" {
    chain_fixture
    signed_manifest "qdistro $PIN
qdistro $NEXT"
    local rr="$WORK/opt-src"
    run_chain "" "$rr" "0x$(key_fpr)"
    [ "$status" -ne 0 ]
    [[ "$output" == *"verification FAILED"* ]] || { echo "$output" >&2; return 1; }
    [ ! -e "$rr" ]
}

# --- pin / checkout failures ---------------------------------------------
@test "chain: a signed pin absent from the repository is fatal" {
    chain_fixture
    # A real commit SHA, but from an unrelated repository.
    local other="$WORK/other"; mkdir -p "$other"; git -C "$other" init -q
    printf 'o\n' > "$other/f"; git -C "$other" add -A; git -C "$other" commit -q -m other
    signed_manifest "qdistro $(git -C "$other" rev-parse HEAD)"
    run_chain "" "$WORK/opt-src"
    [ "$status" -ne 0 ]
    [[ "$output" == *"not present in checkout"* ]] || { echo "$output" >&2; return 1; }
}

@test "chain: moved tag (tag != signed pin) is fatal" {
    chain_fixture
    signed_manifest "qdistro $PIN tag=v1.0.0"
    local rr="$WORK/staged"; stage_checkout "$rr"
    git -C "$rr" tag -f v1.0.0 "$NEXT" >/dev/null
    run_chain skip "$rr"
    [ "$status" -ne 0 ]
    [[ "$output" == *"tampered manifest or moved tag"* ]] || { echo "$output" >&2; return 1; }
}

@test "chain: an untracked file inside a component makes the pinned tree DIRTY (fatal)" {
    chain_fixture
    signed_manifest "qdistro $PIN"
    local rr="$WORK/staged"; stage_checkout "$rr"
    printf 'extra\n' > "$rr/qdwin/extra.c"
    run_chain skip "$rr"
    [ "$status" -ne 0 ]
    [[ "$output" == *"working tree is DIRTY"* && "$output" == *"qdwin/extra.c"* ]] || { echo "$output" >&2; return 1; }
}

@test "chain: a modified tracked file in a renamed component (qdterm/) is DIRTY (fatal)" {
    chain_fixture
    signed_manifest "qdistro $PIN"
    local rr="$WORK/staged"; stage_checkout "$rr"
    printf '# edit\n' >> "$rr/qdterm/pyproject.toml"
    run_chain skip "$rr"
    [ "$status" -ne 0 ]
    [[ "$output" == *"working tree is DIRTY"* && "$output" == *"qdterm/pyproject.toml"* ]] || { echo "$output" >&2; return 1; }
}

@test "chain: a git status that fails is a failed assertion, not a clean tree" {
    chain_fixture
    signed_manifest "qdistro $PIN"
    local rr="$WORK/staged"; stage_checkout "$rr"
    # Shim git: every call passes through except `status`, which errors.
    local shim="$WORK/shim" real; real="$(command -v git)"; mkdir -p "$shim"
    cat > "$shim/git" <<SHIM
#!/bin/bash
for a in "\$@"; do [ "\$a" = status ] && { echo "fatal: shimmed status failure" >&2; exit 128; }; done
exec "$real" "\$@"
SHIM
    chmod 0755 "$shim/git"
    CHAIN_PATH="$shim:$PATH" run_chain skip "$rr"
    [ "$status" -ne 0 ]
    [[ "$output" == *"could not compute working-tree status"* ]] || { echo "$output" >&2; return 1; }
}

# --- trust gate (before any git runs in the tree) -------------------------
@test "chain: a group-writable component directory is refused by the trust gate" {
    chain_fixture
    signed_manifest "qdistro $PIN"
    local rr="$WORK/staged"; stage_checkout "$rr"
    chmod g+w "$rr/qdwin"
    run_chain skip "$rr"
    [ "$status" -ne 0 ]
    [[ "$output" == *"writable by group/other"* ]] || { echo "$output" >&2; return 1; }
}

@test "chain: an absolute symlink inside the tree is refused by the trust gate" {
    chain_fixture
    signed_manifest "qdistro $PIN"
    local rr="$WORK/staged"; stage_checkout "$rr"
    ln -s /etc "$rr/qdwin/escape"
    run_chain skip "$rr"
    [ "$status" -ne 0 ]
    [[ "$output" == *"is absolute or contains '..'"* ]] || { echo "$output" >&2; return 1; }
}

@test "chain: a '..' symlink inside the tree is refused by the trust gate" {
    chain_fixture
    signed_manifest "qdistro $PIN"
    local rr="$WORK/staged"; stage_checkout "$rr"
    ln -s ../../.. "$rr/qdshell/up"
    run_chain skip "$rr"
    [ "$status" -ne 0 ]
    [[ "$output" == *"is absolute or contains '..'"* ]] || { echo "$output" >&2; return 1; }
}

# --- roots that are not a git checkout ------------------------------------
@test "chain: a populated NON-git root (image-style overlay) is refused, with and without --skip-sources" {
    chain_fixture
    signed_manifest "qdistro $PIN"
    local rr="$WORK/overlay"; mkdir -p "$rr"
    git -C "$ORIGIN" archive "$PIN" | tar -x -C "$rr"
    run_chain skip "$rr"
    [ "$status" -ne 0 ]
    [[ "$output" == *"needs a git checkout"* ]] || { echo "skip: $output" >&2; return 1; }
    run_chain "" "$rr"                             # repo_present -> existing tree path
    [ "$status" -ne 0 ]
    [[ "$output" == *"needs a git checkout"* ]] || { echo "fresh: $output" >&2; return 1; }
}

@test "chain: a linked worktree root stays rejected in hardened profiles" {
    chain_fixture
    signed_manifest "qdistro $PIN"
    local main="$WORK/main"; stage_checkout "$main"
    git -C "$main" worktree add -q --detach "$WORK/wt" "$PIN"
    run_chain skip "$WORK/wt"
    [ "$status" -ne 0 ]
    [[ "$output" == *"needs a git checkout"* ]] || { echo "$output" >&2; return 1; }
}

# --- after acquisition: the paths the install steps use --------------------
@test "chain: pip step installs qterminator/qfileman from qdterm/ and qdfileman/ of the pinned tree" {
    chain_fixture
    signed_manifest "qdistro $PIN"
    local rr="$WORK/staged"; stage_checkout "$rr"
    run env QDISTRO_PROFILE=release \
        QDISTRO_SOURCE_MANIFEST="$MANIFEST" \
        QDISTRO_SOURCE_MANIFEST_SIG="$MANIFEST.sig" \
        QDISTRO_RELEASE_KEYRING="$WORK/keyring.gpg" \
        QDISTRO_OPT_PREFIX="$WORK/opt-prefix" \
        bash -c '
            . "$1" >/dev/null 2>&1
            REPO_ROOT="$2"; SKIP_SOURCES=1
            fetch_sources >/dev/null 2>&1 || { echo FETCH_FAILED; exit 9; }
            # Record instead of building/installing (no root, no network).
            python3() { if [ "$1" = -m ]; then echo "PIP ${*: -1}"; fi; return 0; }
            build_qtermwidget_binding() { :; }
            install_app_desktop_assets() { echo "ASSETS $REPO_ROOT/$(comp_dir "$1")"; }
            pip_install_apps
        ' chain "$(BOOT)" "$rr"
    [ "$status" -eq 0 ] || { echo "status=$status: $output" >&2; return 1; }
    [[ "$output" == *"PIP $rr/qdterm"* ]]    || { echo "$output" >&2; return 1; }
    [[ "$output" == *"PIP $rr/qdfileman"* ]] || { echo "$output" >&2; return 1; }
    [[ "$output" == *"ASSETS $rr/qdterm"* && "$output" == *"ASSETS $rr/qdfileman"* ]] || { echo "$output" >&2; return 1; }
    [[ "$output" != *"$rr/qterminator"* && "$output" != *"$rr/qfileman"* ]] || { echo "old dir names used: $output" >&2; return 1; }
}

@test "chain: every installer-chain step resolves inside THIS monorepo (script + source dir)" {
    local name installer suffix n=0
    while IFS='|' read -r name installer suffix; do
        [ -n "$name" ] || continue
        n=$((n + 1))
        [ -f "$SRC_ROOT/$installer" ] || { echo "[$name] missing installer $installer" >&2; return 1; }
        [ -z "$suffix" ] || [ -d "$SRC_ROOT$suffix" ] || { echo "[$name] missing source dir $suffix" >&2; return 1; }
    done < <(bash -c '. "$1" >/dev/null 2>&1; installer_chain_entries' chain "$(BOOT)")
    [ "$n" -ge 10 ] || { echo "only $n chain entries parsed" >&2; return 1; }
}
