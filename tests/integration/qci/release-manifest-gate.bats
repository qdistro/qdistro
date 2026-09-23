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
# fetch set, which since the monorepo migration is the ONE qdistro repository
# (every component in-tree); per-component pins are rejected by the linter.

setup() {
    REPO_ROOT_SRC="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    QCI="$REPO_ROOT_SRC/ci/bin/qci"
    [ -x "$QCI" ] || { echo "qci not found at $QCI" >&2; return 1; }

    RUNS="$(mktemp -d)"
    export QCI_RUNS_DIR="$RUNS"

    RR="$(mktemp -d)"
    # The monorepo checkout under test (the manifest/keyring live beside it,
    # outside the tree, so they never make it dirty).
    MONO="$RR/qdistro"
    export QDISTRO_REPO_ROOT="$MONO"
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

# Create the monorepo at $MONO (root README + in-tree component dirs) with one
# commit; set P_qdistro to its pin.
make_core() {
    mkdir -p "$MONO"
    git -C "$MONO" init -q
    git -C "$MONO" config user.email t@t.invalid
    git -C "$MONO" config user.name t
    echo qdistro > "$MONO/README"
    local c
    for c in qdwin qdshell; do mkdir -p "$MONO/$c"; echo "$c" > "$MONO/$c/README"; done
    git -C "$MONO" add -A
    git -C "$MONO" commit -q -m init
    P_qdistro=$(git -C "$MONO" rev-parse HEAD)
}

# Write a clean, complete, valid base manifest (no tags): the one pin.
base_manifest() {
    printf 'qdistro %s\n' "$P_qdistro" > "$MANIFEST"
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
    row_is "completeness:qdistro" ""   # no completeness failure row
}

# -------------------------------------------------------------------------
# Pass path with tags + a real, signer-bound signature
# -------------------------------------------------------------------------

@test "release-manifest: pinned+tagged clean repos with signer-bound signature => pass" {
    make_core
    git -C "$MONO" tag v1.0.0
    printf 'qdistro %s tag=v1.0.0\n' "$P_qdistro" > "$MANIFEST"
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
    git -C "$MONO" tag v1.0.0
    printf 'qdistro %s tag=v1.0.0\n' "$P_qdistro" > "$MANIFEST"
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
    printf 'qdistro %s\n' "1111111111111111111111111111111111111111" > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is "pin:qdistro" fail
    row_note_has "pin:qdistro" "!= pinned"
}

@test "release-manifest: dirty working tree => fail 15" {
    make_core
    base_manifest
    echo dirty > "$MONO/qdwin/extra"     # untracked, inside a component => not clean
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is "pin:qdistro" fail
    row_note_has "pin:qdistro" "working tree not clean"
}

@test "release-manifest: moved tag (HEAD==pin, tag elsewhere) => fail 15" {
    make_core
    # Second commit, tag points there, but HEAD+pin stay on commit 1.
    echo more > "$MONO/qdwin/x"; git -C "$MONO" add -A
    git -C "$MONO" commit -q -m second
    local c2; c2=$(git -C "$MONO" rev-parse HEAD)
    git -C "$MONO" reset --hard -q "$P_qdistro"   # HEAD back to commit 1, clean
    git -C "$MONO" tag v1.0.0 "$c2"               # tag on commit 2
    printf 'qdistro %s tag=v1.0.0\n' "$P_qdistro" > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is "pin:qdistro" fail
    row_note_has "pin:qdistro" "tamper/moved tag"   # isolates the moved-tag guard
}

@test "release-manifest: unsafe tag name => fail 15" {
    make_core
    printf 'qdistro %s tag=../evil\n' "$P_qdistro" > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_note_has "pin:qdistro" "unsafe tag name"    # the gate's defense-in-depth guard fired
}

@test "release-manifest: a pre-monorepo per-component manifest => lint fail 15" {
    # qdwin/qdshell are in-tree components now; pinning them separately is a
    # malformed manifest (and could never be satisfied by one checkout).
    make_core
    git -C "$MONO" tag v1.0.0
    printf 'qdistro %s tag=v1.0.0\nqdwin %s tag=v1.0.0\nqdshell %s tag=v2.0.0\n' \
        "$P_qdistro" "$P_qdistro" "$P_qdistro" > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is lint fail
}

@test "release-manifest: version core ignores v/V prefix (V1.0.0 is a valid release tag)" {
    make_core
    git -C "$MONO" tag V1.0.0
    printf 'qdistro %s tag=V1.0.0\n' "$P_qdistro" > "$MANIFEST"
    run_gate
    [ "$status" -eq 0 ] || { echo "status=$status: $output" >&2; cat "$RESULTS" >&2; return 1; }
    row_is version-consistency pass
}

@test "release-manifest: missing repo checkout => fail 15 (no git checkout)" {
    make_core
    rm -rf "$MONO/.git"                # pinned in manifest but not a checkout on disk
    base_manifest
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is "pin:qdistro" fail
    row_note_has "pin:qdistro" "no git checkout"   # isolates the .git guard
}

@test "release-manifest: missing required core repo from manifest => completeness fail 15" {
    make_core
    # An active line that is not the qdistro pin (a legacy component name):
    # the manifest is populated, yet the required repo is unpinned.
    printf 'qdwin %s\n' "$P_qdistro" > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is "completeness:qdistro" fail
}

@test "release-manifest: non-hex pin => fail 15" {
    make_core
    printf 'qdistro zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz\n' > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_note_has "pin:qdistro" "not a 40-hex"        # the gate's own hex guard fired
}

@test "release-manifest: malformed manifest => lint fail 15" {
    make_core
    printf 'qdistro deadbeef\n' > "$MANIFEST"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is lint fail
}

@test "release-manifest: tampered manifest fails the signature sub-check => fail 15" {
    make_core
    base_manifest
    sign_manifest
    printf 'qdistro %s tag=v9.9.9\n' "$P_qdistro" > "$MANIFEST"   # valid shape, breaks the sig
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

# -------------------------------------------------------------------------
# Linked worktrees (the documented per-task workflow) and the exact-root rule
# -------------------------------------------------------------------------

@test "release-manifest: clean linked worktree at the pin => pass (.git is a file there)" {
    make_core
    git -C "$MONO" worktree add -q --detach "$RR/wt" "$P_qdistro"
    [ -f "$RR/wt/.git" ]                          # a linked worktree, not a .git dir
    export QDISTRO_REPO_ROOT="$RR/wt"
    base_manifest
    sign_manifest
    export QDISTRO_RELEASE_KEYRING="$RR/keyring.gpg"
    export QDISTRO_MANIFEST_SIG="$MANIFEST.sig"
    export QDISTRO_RELEASE_SIGNER="$FPR"
    run_gate
    [ "$status" -eq 0 ] || { echo "status=$status: $output" >&2; cat "$RESULTS" >&2; return 1; }
    row_is "pin:qdistro" pass
    row_is signature pass
}

@test "release-manifest: a linked worktree is still checked (dirty worktree => fail 15)" {
    make_core
    git -C "$MONO" worktree add -q --detach "$RR/wt" "$P_qdistro"
    export QDISTRO_REPO_ROOT="$RR/wt"
    echo wip > "$RR/wt/qdshell/wip"
    base_manifest
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is "pin:qdistro" fail
    row_note_has "pin:qdistro" "working tree not clean"
}

@test "release-manifest: a directory INSIDE the checkout is not the repo root => fail 15" {
    # <mono>/qdwin resolves to the enclosing repository; judging it by that
    # repo's HEAD would pin the wrong tree. The root must be the top level.
    make_core
    export QDISTRO_REPO_ROOT="$MONO/qdwin"
    base_manifest
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is "pin:qdistro" fail
    row_note_has "pin:qdistro" "no git checkout rooted at"
}
