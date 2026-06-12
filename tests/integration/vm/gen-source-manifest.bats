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

# Call an arbitrary no-arg-or-args bootstrap function with SOURCE_MANIFEST
# pointed at $1; remaining args are passed to the function named $2.
real_boot_fn() {
    local manifest="$1" fn="$2"; shift 2
    QDISTRO_SOURCE_MANIFEST="$manifest" bash -c '
        set -euo pipefail
        . "'"$BOOT"'" >/dev/null 2>&1
        '"$fn"' "$@"
    ' _ "$@"
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

@test "lint: rejects a bare (non key=value) trailing field" {
    local mf="$WORK/extra.txt"
    printf 'qdistro\t%040d\textra\n' 0 > "$mf"
    run "$GEN" --lint "$mf"
    [ "$status" -ne 0 ]
    [[ "$output" == *"not key=value"* ]]
}

# --- extended release-metadata fields (tag=/artifact=/signer=) ----------
@test "lint: accepts tag=/artifact=/signer= fields after the SHA" {
    local mf="$WORK/ext.txt"
    {
        printf 'qdistro\t%040d\ttag=v1.0.0\n' 0
        printf 'qdwin\t%040d\tartifact=sha256:%064d signer=0xDEADBEEFCAFE\n' 1 0
        printf 'qdshell\t%040d\ttag=qdshell-1.2 artifact=sha512:%0128d signer=rel@qdistro.invalid\n' 2 0
    } > "$mf"
    run "$GEN" --lint "$mf"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}

@test "lint: extension fields do not change the parsed pin (manifest_pin reads \$2)" {
    local mf="$WORK/ext2.txt"
    printf 'qdistro\t%040d\ttag=v1.0.0 signer=0xABCD\n' 7 > "$mf"
    local pin
    pin="$(real_manifest_pin "$mf" qdistro)"
    [ "$pin" = "$(printf '%040d' 7)" ] || { echo "pin was '$pin'" >&2; return 1; }
}

@test "lint: rejects an unknown field key" {
    local mf="$WORK/badkey.txt"
    printf 'qdistro\t%040d\tbranch=main\n' 0 > "$mf"
    run "$GEN" --lint "$mf"
    [ "$status" -ne 0 ]
    [[ "$output" == *"unknown field key"* ]]
}

@test "lint: rejects a malformed artifact digest" {
    local mf="$WORK/badart.txt"
    printf 'qdistro\t%040d\tartifact=sha256:nothex\n' 0 > "$mf"
    run "$GEN" --lint "$mf"
    [ "$status" -ne 0 ]
    [[ "$output" == *"malformed"* ]]
}

@test "lint: rejects a duplicate field key on one line" {
    local mf="$WORK/dupkey.txt"
    printf 'qdistro\t%040d\ttag=a tag=b\n' 0 > "$mf"
    run "$GEN" --lint "$mf"
    [ "$status" -ne 0 ]
    [[ "$output" == *"duplicate"* ]]
}

@test "lint: a field value is NOT glob-expanded against the cwd" {
    # Regression guard for `read -ra` vs `set -- \$raw`: with `set --`, the token
    # `tag=*` would glob-expand to a real file named `tag=v1.0.0` in the cwd and
    # the line would WRONGLY lint clean, diverging from the bootstrap awk (which
    # never globs). With read -ra the token stays `tag=*` -> malformed -> reject.
    local d="$WORK/globdir"; mkdir -p "$d"
    : > "$d/tag=v1.0.0"    # decoy a glob could match
    local mf="$d/m.txt"
    printf 'qdistro\t%040d\ttag=*\n' 0 > "$mf"
    cd "$d"                # lint runs with this as cwd
    run "$GEN" --lint "$mf"
    [ "$status" -ne 0 ] || { echo "tag=* must not glob-expand to the decoy file" >&2; return 1; }
    [[ "$output" == *"malformed"* ]] || { echo "$output" >&2; return 1; }
}

@test "gen: --tags emits tag= for a tagged HEAD and --signer stamps every line" {
    stub_all
    git -C "$WORK/qdistro" tag v9.9.9
    local mf="$WORK/m.txt"
    run "$GEN" --repo-root "$WORK" --tags --signer 0xFEEDFACE -o "$mf"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    # The tagged repo carries tag=; every non-comment line carries signer=.
    grep -qE "^qdistro	${EXPECTED[qdistro]} tag=v9.9.9" "$mf" \
        || { echo "missing tag= on qdistro line:"; grep qdistro "$mf" >&2; return 1; }
    local n_lines n_signed
    n_lines="$(awk '/^[[:space:]]*#/{next} NF' "$mf" | wc -l)"
    n_signed="$(grep -c 'signer=0xFEEDFACE' "$mf")"
    [ "$n_lines" -eq "$n_signed" ] || { echo "signed $n_signed of $n_lines lines" >&2; return 1; }
    # And the result still lints clean + round-trips through the parser.
    run "$GEN" --lint "$mf"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}

@test "gen: --require-tags fails when HEAD is untagged" {
    stub_all
    run "$GEN" --repo-root "$WORK" --require-tags
    [ "$status" -ne 0 ]
    [[ "$output" == *"no exact-match tag"* ]]
}

@test "gen: --artifact pins a per-repo digest; unknown repo/format rejected" {
    stub_all
    local mf="$WORK/art.txt"
    run "$GEN" --repo-root "$WORK" --artifact "qdwin=sha256:$(printf '%064d' 0)" -o "$mf"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    grep -qE "^qdwin	${EXPECTED[qdwin]} artifact=sha256:0{64}$" "$mf" \
        || { echo "missing artifact= on qdwin:"; grep qdwin "$mf" >&2; return 1; }
    run "$GEN" --repo-root "$WORK" --artifact "nope=sha256:$(printf '%064d' 0)"
    [ "$status" -ne 0 ]
    [[ "$output" == *"unknown repo"* ]]
    run "$GEN" --repo-root "$WORK" --artifact "qdwin=sha256:short"
    [ "$status" -ne 0 ]
    [[ "$output" == *"well-formed"* ]]
}

# --- bootstrap-side readers (manifest_field / manifest_has_pins) --------
@test "boot: manifest_field reads tag=/signer= (and is empty when absent)" {
    local mf="$WORK/f.txt"
    printf 'qdistro\t%040d\ttag=v1.0.0 signer=0xABCD\nqdwin\t%040d\n' 0 1 > "$mf"
    [ "$(real_boot_fn "$mf" manifest_field qdistro tag)" = "v1.0.0" ]
    [ "$(real_boot_fn "$mf" manifest_field qdistro signer)" = "0xABCD" ]
    # qdwin has no fields; qdistro has no artifact -> both empty.
    [ -z "$(real_boot_fn "$mf" manifest_field qdwin tag)" ]
    [ -z "$(real_boot_fn "$mf" manifest_field qdistro artifact)" ]
}

@test "boot: manifest_has_pins true only for a populated manifest" {
    local empty="$WORK/empty.txt" pinned="$WORK/pinned.txt"
    printf '# only a comment\n#qdistro 000\n' > "$empty"
    run real_boot_fn "$empty" manifest_has_pins
    [ "$status" -ne 0 ] || { echo "stub manifest should have no pins" >&2; return 1; }
    printf 'qdistro\t%040d\ttag=v1.0.0\n' 0 > "$pinned"
    run real_boot_fn "$pinned" manifest_has_pins
    [ "$status" -eq 0 ] || { echo "pinned manifest should report pins" >&2; return 1; }
}

# --- verify_repo_pin tag consistency ------------------------------------
@test "boot: verify_repo_pin accepts a manifest tag that matches the pin" {
    stub_all
    git -C "$WORK/qdistro" tag v1.2.3   # tag points at the pinned HEAD
    local mf="$WORK/m.txt"
    printf 'qdistro\t%s\ttag=v1.2.3\n' "${EXPECTED[qdistro]}" > "$mf"
    run bash -c '
        set -euo pipefail
        export QDISTRO_PROFILE=release QDISTRO_REPO_ROOT="'"$WORK"'" QDISTRO_SOURCE_MANIFEST="'"$mf"'"
        REPO_ROOT="'"$WORK"'"; SOURCE_MANIFEST="'"$mf"'"
        . "'"$BOOT"'" >/dev/null 2>&1
        verify_repo_pin qdistro
    '
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    [[ "$output" == *"(tag v1.2.3)"* ]] || { echo "$output" >&2; return 1; }
}

@test "boot: verify_repo_pin REJECTS an unsafe tag shape (lint-bypass defence)" {
    stub_all
    # A hand-edited / lint-bypassed manifest with a refspec-y tag must be
    # refused before it ever reaches `git refs/tags/$tag`.
    local mf="$WORK/m.txt"
    printf 'qdistro\t%s\ttag=bad:ref\n' "${EXPECTED[qdistro]}" > "$mf"
    run bash -c '
        set -euo pipefail
        export QDISTRO_PROFILE=release QDISTRO_REPO_ROOT="'"$WORK"'" QDISTRO_SOURCE_MANIFEST="'"$mf"'"
        REPO_ROOT="'"$WORK"'"; SOURCE_MANIFEST="'"$mf"'"
        . "'"$BOOT"'" >/dev/null 2>&1
        verify_repo_pin qdistro
    '
    [ "$status" -ne 0 ] || { echo "unsafe tag must be rejected" >&2; return 1; }
    [[ "$output" == *"not a safe tag name"* ]] || { echo "$output" >&2; return 1; }
}

@test "boot: verify_repo_pin is FATAL when the manifest tag != the pin" {
    stub_all
    # Create a tag on a DIFFERENT commit than the pinned one.
    ( cd "$WORK/qdistro"; printf 'x\n' > x; git add x; git commit -q -m other; git tag v1.2.3 )
    local other; other="$(git -C "$WORK/qdistro" rev-parse v1.2.3)"
    [ "$other" != "${EXPECTED[qdistro]}" ]
    local mf="$WORK/m.txt"
    # Pin the ORIGINAL commit but claim tag v1.2.3 (which is on 'other').
    printf 'qdistro\t%s\ttag=v1.2.3\n' "${EXPECTED[qdistro]}" > "$mf"
    run bash -c '
        set -euo pipefail
        export QDISTRO_PROFILE=release QDISTRO_REPO_ROOT="'"$WORK"'" QDISTRO_SOURCE_MANIFEST="'"$mf"'"
        REPO_ROOT="'"$WORK"'"; SOURCE_MANIFEST="'"$mf"'"
        . "'"$BOOT"'" >/dev/null 2>&1
        verify_repo_pin qdistro
    '
    [ "$status" -ne 0 ] || { echo "expected fatal tag mismatch" >&2; return 1; }
    [[ "$output" == *"tag 'v1.2.3' resolves to"* ]] || { echo "$output" >&2; return 1; }
}
