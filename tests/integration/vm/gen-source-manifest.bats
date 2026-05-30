#!/usr/bin/env bats
# Behavioural lock-in for scripts/install/gen-source-manifest.sh, the
# release/packaging tool that pins source-manifest.txt for qdistro-bootstrap.sh.
#
# Needs NO live VM and NO network: it stubs the qdistro source repos with
# `git init` + a commit in a temp dir, runs the generator, and feeds its
# output back through the REAL bootstrap parser (manifest_pin) and the real
# verify_repo_pin to prove byte-compatibility (round-trip).
#
# What it pins:
#   - Correct repo set (the 9 repos fetch_sources fetches), in order.
#   - Generated output parses IDENTICALLY through the bootstrap's manifest_pin
#     awk path: the pin for each repo == that repo's stubbed HEAD SHA (exact).
#   - verify_repo_pin accepts a generated manifest end-to-end (real checkout).
#   - Dirty-repo refusal (fail-closed), with --allow-dirty override.
#   - Missing-repo handling: error by default, skip with --allow-missing.
#   - --lint accepts a generated manifest and rejects malformed/dup/unknown.
#
# Run: bats tests/integration/vm/gen-source-manifest.bats

setup() {
    SRC_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" \
                    rev-parse --show-toplevel 2>/dev/null)"
    GEN="$SRC_ROOT/scripts/install/gen-source-manifest.sh"
    BOOT="$SRC_ROOT/scripts/install/qdistro-bootstrap.sh"
    [ -f "$GEN" ]  || { echo "generator not found at $GEN" >&2; return 1; }
    [ -f "$BOOT" ] || { echo "bootstrap not found at $BOOT" >&2; return 1; }

    REPOS=(qdistro qdwin qdshell qdlocker qdbrowser qdgreeter qterminator qnotebook qfileman)

    WORK="$BATS_TEST_TMPDIR/root"
    mkdir -p "$WORK"
    # Hermetic git identity so commits work without host config.
    export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t
    export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
    export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
}

# Stub one git repo at $WORK/<repo> with a single commit; echo its HEAD SHA.
stub_repo() {
    local repo="$1" dir="$WORK/$1"
    mkdir -p "$dir"
    git -C "$dir" init -q
    printf '%s\n' "$repo" > "$dir/README"
    git -C "$dir" add README
    git -C "$dir" commit -q -m "init $repo"
    git -C "$dir" rev-parse HEAD
}

# Stub all 9 repos; populate EXPECTED[<repo>]=<sha>.
stub_all() {
    declare -gA EXPECTED=()
    local r
    for r in "${REPOS[@]}"; do
        EXPECTED[$r]="$(stub_repo "$r")"
    done
}

# Parse a manifest through the REAL bootstrap parser: source the bootstrap
# (it guards main() under BASH_SOURCE==$0) and call its manifest_pin with
# SOURCE_MANIFEST pointed at $1. Prints the pin for repo $2.
real_manifest_pin() {
    local manifest="$1" repo="$2"
    # The bootstrap sets SOURCE_MANIFEST from QDISTRO_SOURCE_MANIFEST at
    # source-time, so point it via that env var (a bare SOURCE_MANIFEST would
    # be overwritten when we source the bootstrap).
    QDISTRO_SOURCE_MANIFEST="$manifest" bash -c '
        set -euo pipefail
        . "'"$BOOT"'" >/dev/null 2>&1
        manifest_pin "'"$repo"'"
    '
}

# --- syntax -------------------------------------------------------------
@test "gen: generator is valid bash" {
    run bash -n "$GEN"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}

# --- repo set -----------------------------------------------------------
@test "gen: emits exactly the 9 bootstrap repos, in fetch order" {
    stub_all
    run "$GEN" --repo-root "$WORK"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }

    # Field-1 of every non-comment line, in order.
    local got
    got="$(printf '%s\n' "$output" | awk '/^[[:space:]]*#/{next} NF{print $1}')"
    [ "$got" = "$(printf '%s\n' "${REPOS[@]}")" ] \
        || { echo "repo set/order mismatch:"$'\n'"$got" >&2; return 1; }
}

# --- round-trip through the REAL bootstrap parser -----------------------
@test "gen: output parses identically via bootstrap manifest_pin (exact SHAs)" {
    stub_all
    local mf="$WORK/manifest.txt"
    run "$GEN" --repo-root "$WORK" -o "$mf"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }

    local r pin
    for r in "${REPOS[@]}"; do
        pin="$(real_manifest_pin "$mf" "$r")"
        [ "$pin" = "${EXPECTED[$r]}" ] \
            || { echo "$r: bootstrap parsed '$pin' != stubbed HEAD '${EXPECTED[$r]}'" >&2; return 1; }
    done
}

@test "gen: every emitted pin is a lowercase 40-hex SHA on a TAB-separated line" {
    stub_all
    local mf="$WORK/m.txt"
    run "$GEN" --repo-root "$WORK" -o "$mf"
    [ "$status" -eq 0 ]
    # Each non-comment line must be EXACTLY: <repo><TAB><40 hex>, no trailing
    # junk. Split on TAB (FS="\t"): NF must be 2, $1 lowercase-alnum, $2 40-hex.
    run awk -F'\t' '
        /^[[:space:]]*#/ { next }
        !NF || $0=="" { next }
        { lines++
          if (NF != 2)                       { print "not TAB-2-field: " $0; bad=1 }
          else if ($1 !~ /^[a-z0-9]+$/)      { print "bad repo: " $1; bad=1 }
          else if ($2 !~ /^[0-9a-f]{40}$/)   { print "bad sha: " $2; bad=1 } }
        END { if (lines != 9) { print "expected 9 lines, got " lines; bad=1 }
              exit bad ? 1 : 0 }
    ' "$mf"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}

# --- end-to-end with real verify_repo_pin -------------------------------
@test "gen: generated manifest is accepted end-to-end by verify_repo_pin" {
    stub_all
    local mf="$WORK/manifest.txt"
    "$GEN" --repo-root "$WORK" -o "$mf"

    # Move each stub HEAD forward, then have verify_repo_pin (hardened profile)
    # detach back to the pinned commit. It must succeed AND land on the pin.
    local r
    for r in "${REPOS[@]}"; do
        ( cd "$WORK/$r"
          printf 'moved\n' > moved
          git add moved && git commit -q -m moved )
    done

    run bash -c '
        set -euo pipefail
        export QDISTRO_PROFILE=release
        export QDISTRO_REPO_ROOT="'"$WORK"'"
        export QDISTRO_SOURCE_MANIFEST="'"$mf"'"
        SOURCE_MANIFEST="'"$mf"'"
        REPO_ROOT="'"$WORK"'"
        . "'"$BOOT"'" >/dev/null 2>&1
        # resolve_profile populated by sourcing; ensure hardened path active.
        for r in '"${REPOS[*]}"'; do
            verify_repo_pin "$r" >/dev/null || { echo "verify_repo_pin failed for $r"; exit 1; }
        done
        echo ALL_VERIFIED
    '
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    [[ "$output" == *ALL_VERIFIED* ]] || { echo "$output" >&2; return 1; }

    # And every checkout is now detached at exactly its pinned SHA.
    for r in "${REPOS[@]}"; do
        [ "$(git -C "$WORK/$r" rev-parse HEAD)" = "${EXPECTED[$r]}" ] \
            || { echo "$r not detached at pin" >&2; return 1; }
    done
}

# --- dirty-repo refusal -------------------------------------------------
@test "gen: refuses a DIRTY repo (fail-closed) by default" {
    stub_all
    # Dirty qdshell with an untracked file.
    printf 'wip\n' > "$WORK/qdshell/wip.txt"

    run "$GEN" --repo-root "$WORK"
    [ "$status" -ne 0 ] || { echo "expected failure, got 0:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *qdshell*DIRTY* ]] || { echo "wrong error: $output" >&2; return 1; }
}

@test "gen: --allow-dirty pins a dirty repo's HEAD anyway" {
    stub_all
    printf 'wip\n' > "$WORK/qdshell/wip.txt"   # untracked, does not change HEAD

    run "$GEN" --repo-root "$WORK" --allow-dirty
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    # The pinned SHA is still the committed HEAD (untracked file isn't part of it).
    local pin
    pin="$(printf '%s\n' "$output" | awk '$1=="qdshell"{print $2}')"
    [ "$pin" = "${EXPECTED[qdshell]}" ] || { echo "pin $pin != ${EXPECTED[qdshell]}" >&2; return 1; }
}

@test "gen: tracked modification also counts as dirty" {
    stub_all
    printf 'changed\n' >> "$WORK/qdwin/README"   # modify a tracked file
    run "$GEN" --repo-root "$WORK"
    [ "$status" -ne 0 ]
    [[ "$output" == *qdwin*DIRTY* ]] || { echo "$output" >&2; return 1; }
}

# --- missing-repo handling ----------------------------------------------
@test "gen: errors on a missing repo by default" {
    stub_all
    rm -rf "$WORK/qfileman"
    run "$GEN" --repo-root "$WORK"
    [ "$status" -ne 0 ] || { echo "expected failure:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *qfileman*"no git checkout"* ]] || { echo "$output" >&2; return 1; }
}

@test "gen: errors on a non-git directory by default" {
    stub_all
    rm -rf "$WORK/qnotebook/.git"   # plain dir, not a git checkout
    run "$GEN" --repo-root "$WORK"
    [ "$status" -ne 0 ]
    [[ "$output" == *qnotebook*"no git checkout"* ]] || { echo "$output" >&2; return 1; }
}

@test "gen: --allow-missing skips an absent repo and emits the rest" {
    stub_all
    rm -rf "$WORK/qfileman"
    run "$GEN" --repo-root "$WORK" --allow-missing
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    # qfileman absent; the other 8 present and parseable.
    run bash -c 'printf "%s\n" "$1" | awk "/^[[:space:]]*#/{next} NF{print \$1}"' _ "$output"
    [[ "$output" != *qfileman* ]] || { echo "qfileman should be skipped" >&2; return 1; }
    [[ "$output" == *qdistro* ]] && [[ "$output" == *qnotebook* ]]
}

# --- header -------------------------------------------------------------
@test "gen: output carries a GENERATED header comment (ignored by parser)" {
    stub_all
    run "$GEN" --repo-root "$WORK"
    [ "$status" -eq 0 ]
    [[ "$output" == *"GENERATED by scripts/install/gen-source-manifest.sh"* ]]
    # Header lines are comments -> parser skips them; manifest_pin of a header
    # token returns empty.
    local mf="$WORK/m.txt"; printf '%s\n' "$output" > "$mf"
    [ -z "$(real_manifest_pin "$mf" GENERATED)" ]
}

# --- lint mode ----------------------------------------------------------
@test "lint: accepts a freshly generated manifest" {
    stub_all
    local mf="$WORK/manifest.txt"
    "$GEN" --repo-root "$WORK" -o "$mf"
    run "$GEN" --lint "$mf"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    [[ "$output" == *OK* ]]
}

@test "lint: accepts the committed (all-commented) source-manifest.txt" {
    # The shipped manifest has only comments -> zero pinned lines, still valid.
    run "$GEN" --lint "$SRC_ROOT/scripts/install/source-manifest.txt"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}

@test "lint: rejects a non-40-hex SHA" {
    local mf="$WORK/bad.txt"
    printf 'qdistro\tdeadbeef\n' > "$mf"
    run "$GEN" --lint "$mf"
    [ "$status" -ne 0 ]
    [[ "$output" == *"not a 40-hex"* ]]
}

@test "lint: rejects an unknown repo name" {
    local mf="$WORK/bad.txt"
    printf 'bogusrepo\t%040d\n' 0 > "$mf"
    run "$GEN" --lint "$mf"
    [ "$status" -ne 0 ]
    [[ "$output" == *"unknown repo"* ]]
}

@test "lint: rejects a duplicate repo pin" {
    local mf="$WORK/dup.txt"
    printf 'qdistro\t%040d\nqdistro\t%040d\n' 0 0 > "$mf"
    run "$GEN" --lint "$mf"
    [ "$status" -ne 0 ]
    [[ "$output" == *"duplicate pin"* ]]
}

@test "lint: rejects a line with a stray third field" {
    local mf="$WORK/extra.txt"
    printf 'qdistro\t%040d\textra\n' 0 > "$mf"
    run "$GEN" --lint "$mf"
    [ "$status" -ne 0 ]
    [[ "$output" == *"<repo> <40-hex-sha>"* ]]
}
