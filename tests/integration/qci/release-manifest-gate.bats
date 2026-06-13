#!/usr/bin/env bats
#
# Host-only tests for the `qci release-manifest` gate (R1;
# 05-agent-test-plan.md §A). NO VM, NO network: synthetic git checkouts under a
# temp QDISTRO_REPO_ROOT, a manifest pinning them via QDISTRO_RELEASE_MANIFEST,
# and a throwaway GPG home for the signature sub-check. Drives the REAL qci
# runner with QCI_RUNS_DIR pointed at a temp dir and asserts both the gate's
# exit status AND the per-subject rows (status + notes) it records in
# results.tsv.
#
# The gate is READ-ONLY (it must never check out a pin), fails CLOSED on a
# populated-but-divergent manifest (EXIT_RELEASE=15), and only records `blocked`
# (exit 0) for genuinely-absent release inputs (unpopulated manifest / no
# keyring on a dev host). A release-grade manifest must pin the bootstrap's
# fatal fetch set: qdistro qdwin qdshell.

setup() {
    REPO_ROOT_SRC="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    QCI="$REPO_ROOT_SRC/ci/bin/qci"
    [ -x "$QCI" ] || { echo "qci not found at $QCI" >&2; return 1; }

    RUNS="$(mktemp -d)"
    export QCI_RUNS_DIR="$RUNS"

    RR="$(mktemp -d)"
    export QDISTRO_REPO_ROOT="$RR"
    MANIFEST="$RR/source-manifest.txt"
    export QDISTRO_RELEASE_MANIFEST="$MANIFEST"

    GNUPGHOME="$RR/gnupg"; export GNUPGHOME
    mkdir -p "$GNUPGHOME"; chmod 0700 "$GNUPGHOME"
    KEY_USER="Qdistro Test Release <release-test@qdistro.invalid>"
    gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
        --quick-generate-key "$KEY_USER" ed25519 sign 0
    gpg --batch --quiet --export "$KEY_USER" > "$RR/keyring.gpg"
    FPR=$(gpg --batch --with-colons --fingerprint "$KEY_USER" \
              | awk -F: '/^fpr:/{print $10; exit}')
}

teardown() {
    rm -rf "$RUNS" "$RR"
}

# Create a git repo at $RR/<name> with one commit; echo its commit SHA.
mkrepo() {
    local name="$1"
    local dir="$RR/$name"
    mkdir -p "$dir"
    git -C "$dir" init -q
    git -C "$dir" config user.email t@t.invalid
    git -C "$dir" config user.name t
    echo "$name" > "$dir/README"
    git -C "$dir" add -A
    git -C "$dir" commit -q -m init
    git -C "$dir" rev-parse HEAD
}

# Create the three core repos; set P_qdistro / P_qdwin / P_qdshell to their pins.
make_core() {
    P_qdistro=$(mkrepo qdistro)
    P_qdwin=$(mkrepo qdwin)
    P_qdshell=$(mkrepo qdshell)
}

# Write a clean, complete, valid base manifest (no tags) pinning the core set.
base_manifest() {
    printf 'qdistro %s\nqdwin %s\nqdshell %s\n' \
        "$P_qdistro" "$P_qdwin" "$P_qdshell" > "$MANIFEST"
}

sign_manifest() {
    gpg --batch --quiet --yes --pinentry-mode loopback --passphrase '' \
        --local-user "$KEY_USER" --detach-sign --output "$MANIFEST.sig" "$MANIFEST"
}

# Run the gate; sets $status/$output and exposes $RESULTS (the run's results.tsv).
run_gate() {
    run "$QCI" release-manifest
    local latest
    latest=$(find "$RUNS" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
                 | sort -nr | awk 'NR==1{print $2}')
    RESULTS="$latest/results.tsv"
}

# Assert results.tsv has a row for subject $1 with status $2 (no pipe — a
# `grep -q` short-circuit would SIGPIPE awk and trip bats's pipefail).
row_is() {
    local subject="$1" want="$2" got
    got=$(awk -F'\t' -v s="$subject" '$2==s {print $3; exit}' "$RESULTS")
    [ "$got" = "$want" ]
}

# Assert the row for subject $1 carries a note (col 8) containing substring $2.
row_note_has() {
    local subject="$1" want="$2" note
    note=$(awk -F'\t' -v s="$subject" '$2==s {print $8; exit}' "$RESULTS")
    case "$note" in *"$want"*) return 0 ;; *) return 1 ;; esac
}

# -------------------------------------------------------------------------
# Blocked-not-fatal paths (dev host posture)
# -------------------------------------------------------------------------

@test "release-manifest: unpopulated manifest is blocked, exit 0" {
    printf '# all comments\n#qdwin 0000\n' > "$MANIFEST"
    run_gate
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    row_is unpopulated blocked
}

@test "release-manifest: missing manifest file is blocked, exit 0" {
    rm -f "$MANIFEST"
    run_gate
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    row_is manifest blocked
}

@test "release-manifest: complete clean repos but no keyring => pass with signature blocked" {
    make_core
    base_manifest
    run_gate
    [ "$status" -eq 0 ] || { echo "$output" >&2; cat "$RESULTS" >&2; return 1; }
    row_is lint pass
    row_is signature blocked
    row_note_has signature "no keyring provided"
    row_is "pin:qdistro" pass
    row_is "pin:qdwin" pass
    row_is "pin:qdshell" pass
}

# -------------------------------------------------------------------------
# Pass path with tags + a real, signer-bound signature
# -------------------------------------------------------------------------

@test "release-manifest: pinned+tagged clean repos with signer-bound signature => pass" {
    make_core
    git -C "$RR/qdistro" tag v1.0.0
    git -C "$RR/qdwin" tag v1.0.0
    git -C "$RR/qdshell" tag v1.0.0
    printf 'qdistro %s tag=v1.0.0\nqdwin %s tag=v1.0.0\nqdshell %s tag=v1.0.0\n' \
        "$P_qdistro" "$P_qdwin" "$P_qdshell" > "$MANIFEST"
    sign_manifest
    export QDISTRO_RELEASE_KEYRING="$RR/keyring.gpg"
    export QDISTRO_MANIFEST_SIG="$MANIFEST.sig"
    export QDISTRO_RELEASE_SIGNER="$FPR"   # exercise authoritative signer binding
    run_gate
    [ "$status" -eq 0 ] || { echo "$output" >&2; cat "$RESULTS" >&2; return 1; }
    row_is signature pass
    row_note_has signature "signer bound"
    row_is version-consistency pass
}

@test "release-manifest: QCI_RELEASE=1 does NOT escalate a fully-signed complete green run" {
    make_core
    git -C "$RR/qdistro" tag v1.0.0
    git -C "$RR/qdwin" tag v1.0.0
    git -C "$RR/qdshell" tag v1.0.0
    printf 'qdistro %s tag=v1.0.0\nqdwin %s tag=v1.0.0\nqdshell %s tag=v1.0.0\n' \
        "$P_qdistro" "$P_qdwin" "$P_qdshell" > "$MANIFEST"
    sign_manifest
    export QDISTRO_RELEASE_KEYRING="$RR/keyring.gpg"
    export QDISTRO_MANIFEST_SIG="$MANIFEST.sig"
    export QDISTRO_RELEASE_SIGNER="$FPR"
    QCI_RELEASE=1 run_gate                  # release-profile mode, but no blocked rows
    [ "$status" -eq 0 ] || { echo "status=$status: $output" >&2; cat "$RESULTS" >&2; return 1; }
    run awk -F'\t' '$1=="release-profile"{print; f=1} END{exit f?1:0}' "$RESULTS"
    [ "$status" -eq 0 ]                     # no release-profile escalation row recorded
}

@test "release-manifest: default-adjacent .sig is found without QDISTRO_MANIFEST_SIG" {
    make_core
    base_manifest
    sign_manifest                      # writes $MANIFEST.sig next to the manifest
    export QDISTRO_RELEASE_KEYRING="$RR/keyring.gpg"
    # NB: QDISTRO_MANIFEST_SIG intentionally unset -> gate defaults to $manifest.sig
    run_gate
    [ "$status" -eq 0 ] || { echo "$output" >&2; cat "$RESULTS" >&2; return 1; }
    row_is signature pass
}

# -------------------------------------------------------------------------
# Hard-fail paths (EXIT_RELEASE = 15) — each isolates ONE guard
# -------------------------------------------------------------------------

@test "release-manifest: wrong commit pin => fail 15 (HEAD!=pin)" {
    make_core
    printf 'qdistro %s\nqdwin %s\nqdshell %s\n' \
        "$P_qdistro" "1111111111111111111111111111111111111111" "$P_qdshell" > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is "pin:qdwin" fail
    row_note_has "pin:qdwin" "!= pinned"
}

@test "release-manifest: dirty working tree => fail 15" {
    make_core
    base_manifest
    echo dirty > "$RR/qdwin/extra"     # untracked => not clean
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is "pin:qdwin" fail
    row_note_has "pin:qdwin" "working tree not clean"
}

@test "release-manifest: moved tag (HEAD==pin, tag elsewhere) => fail 15" {
    make_core
    # Second commit on qdwin, tag points there, but HEAD+pin stay on commit 1.
    echo more > "$RR/qdwin/x"; git -C "$RR/qdwin" add -A
    git -C "$RR/qdwin" commit -q -m second
    local c2; c2=$(git -C "$RR/qdwin" rev-parse HEAD)
    git -C "$RR/qdwin" reset --hard -q "$P_qdwin"   # HEAD back to commit 1, clean
    git -C "$RR/qdwin" tag v1.0.0 "$c2"             # tag on commit 2
    printf 'qdistro %s\nqdwin %s tag=v1.0.0\nqdshell %s\n' \
        "$P_qdistro" "$P_qdwin" "$P_qdshell" > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is "pin:qdwin" fail
    row_note_has "pin:qdwin" "tamper/moved tag"   # isolates the moved-tag guard
}

@test "release-manifest: unsafe tag name => fail 15" {
    make_core
    printf 'qdistro %s\nqdwin %s tag=../evil\nqdshell %s\n' \
        "$P_qdistro" "$P_qdwin" "$P_qdshell" > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_note_has "pin:qdwin" "unsafe tag name"    # the gate's defense-in-depth guard fired
}

@test "release-manifest: inconsistent tag versions => fail 15" {
    make_core
    git -C "$RR/qdistro" tag v1.0.0
    git -C "$RR/qdwin" tag v1.0.0
    git -C "$RR/qdshell" tag v2.0.0
    printf 'qdistro %s tag=v1.0.0\nqdwin %s tag=v1.0.0\nqdshell %s tag=v2.0.0\n' \
        "$P_qdistro" "$P_qdwin" "$P_qdshell" > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is version-consistency fail
}

@test "release-manifest: version core ignores v/V prefix (V1.0.0 == v1.0.0)" {
    make_core
    git -C "$RR/qdistro" tag V1.0.0
    git -C "$RR/qdwin" tag v1.0.0
    git -C "$RR/qdshell" tag v1.0.0
    printf 'qdistro %s tag=V1.0.0\nqdwin %s tag=v1.0.0\nqdshell %s tag=v1.0.0\n' \
        "$P_qdistro" "$P_qdwin" "$P_qdshell" > "$MANIFEST"
    run_gate
    [ "$status" -eq 0 ] || { echo "status=$status: $output" >&2; cat "$RESULTS" >&2; return 1; }
    row_is version-consistency pass
}

@test "release-manifest: missing repo checkout => fail 15 (no git checkout)" {
    make_core
    rm -rf "$RR/qdshell"               # pinned in manifest but absent on disk
    base_manifest
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is "pin:qdshell" fail
    row_note_has "pin:qdshell" "no git checkout"   # isolates the .git guard
}

@test "release-manifest: missing required core repo from manifest => completeness fail 15" {
    make_core
    # Pin only qdwin + qdshell; omit the required core repo qdistro.
    printf 'qdwin %s\nqdshell %s\n' "$P_qdwin" "$P_qdshell" > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is "completeness:qdistro" fail
}

@test "release-manifest: non-hex pin => fail 15" {
    make_core
    printf 'qdistro %s\nqdwin zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz\nqdshell %s\n' \
        "$P_qdistro" "$P_qdshell" > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_note_has "pin:qdwin" "not a 40-hex"        # the gate's own hex guard fired
}

@test "release-manifest: malformed manifest => lint fail 15" {
    make_core
    printf 'qdistro %s\nqdwin deadbeef\nqdshell %s\n' \
        "$P_qdistro" "$P_qdshell" > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is lint fail
}

@test "release-manifest: tampered manifest fails the signature sub-check => fail 15" {
    make_core
    base_manifest
    sign_manifest
    printf 'qdlocker %s\n' "$P_qdwin" >> "$MANIFEST"   # valid shape, breaks the sig
    export QDISTRO_RELEASE_KEYRING="$RR/keyring.gpg"
    export QDISTRO_MANIFEST_SIG="$MANIFEST.sig"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is signature fail
}

@test "release-manifest: wrong expected signer => fail 15 (signer binding)" {
    make_core
    base_manifest
    sign_manifest
    export QDISTRO_RELEASE_KEYRING="$RR/keyring.gpg"
    export QDISTRO_MANIFEST_SIG="$MANIFEST.sig"
    export QDISTRO_RELEASE_SIGNER="0000000000000000000000000000000000000000"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is signature fail
}

@test "release-manifest: keyring supplied but verifier-requested sig missing => fail 15" {
    make_core
    base_manifest                      # no .sig written
    export QDISTRO_RELEASE_KEYRING="$RR/keyring.gpg"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is signature fail
    row_note_has signature "signature file missing"
}
