#!/usr/bin/env bats
#
# Host-only tests for the `qci image` gate's source-ancestry check
# (ci/lib/gates/image.sh image_source_check). NO VM, NO libguestfs: the build
# dir is a fake with only a pre-extracted tree (extracted/etc/qdistro/release),
# so the gate takes its "no published artifact, fall back to the extracted
# tree" path. Drives the REAL qci runner copied into a throwaway git repo whose
# history the test controls (HEAD, an ancestor, and a side-branch commit), and
# asserts the verify-contents row in results.tsv.
#
# Why: a 2026-09-10 sibling-layout bundle left in /var/tmp/qdistro-build
# "failed" verify-contents on the monorepo with EXIT_BUILD, which read like a
# product regression. Policy (astra review of 6c199f7b0):
#   - a VALID stamp whose SHA is not an ancestor, cannot be tied to HEAD
#     (missing object, shallow history, git error), or the recognised legacy
#     five-repo schema: BLOCKED, naming both SHAs and the fix;
#   - INVALID provenance (missing, symlinked, duplicate, malformed): FAIL/20,
#     because the stamp writer is product code under test;
#   - HEAD or an ancestor: the checklist runs; an ancestor's SHA/distance is
#     carried in the row's notes.

setup() {
    SRC="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    T="$(mktemp -d)"
    REPO="$T/repo"
    mkdir -p "$REPO"
    cp -a "$SRC/ci" "$SRC/image" "$REPO/"
    rm -rf "$REPO/ci/runs"
    git -C "$REPO" init -q
    git -C "$REPO" config user.email t@t.invalid
    git -C "$REPO" config user.name t
    git -C "$REPO" add -A
    git -C "$REPO" commit -q -m base
    OLD=$(git -C "$REPO" rev-parse HEAD)
    local main
    main=$(git -C "$REPO" symbolic-ref --short HEAD)
    git -C "$REPO" checkout -q -b side
    echo side > "$REPO/side.txt"; git -C "$REPO" add side.txt
    git -C "$REPO" commit -q -m side
    SIDE=$(git -C "$REPO" rev-parse HEAD)
    git -C "$REPO" checkout -q "$main"
    echo head > "$REPO/head.txt"; git -C "$REPO" add head.txt
    git -C "$REPO" commit -q -m head
    HEAD_SHA=$(git -C "$REPO" rev-parse HEAD)

    BUILD="$T/build"
    mkdir -p "$BUILD/extracted/etc/qdistro"
    export QDISTRO_BUILD_DIR="$BUILD"
    export QCI_RUNS_DIR="$T/runs"
    unset QDISTRO_IMAGE QCI_RELEASE
}

teardown() {
    rm -rf "$T"
}

# stamp <SOURCE lines...> -- write the image's /etc/qdistro/release
stamp() {
    {
        echo 'VERSION=0.1.0'
        echo 'SNAPSHOT=20260902'
        echo 'PROFILE=dev'
        echo 'ARTIFACT=qdistro-0.1.0-20260902.raw.xz'
        printf '%s\n' "$@"
    } > "$BUILD/extracted/etc/qdistro/release"
}

# stub_checker_pass -- make the fixture's checklist pass, so a fail/20 is
# attributable to the gate's provenance policy alone, not the fake tree.
stub_checker_pass() {
    printf '#!/bin/bash\necho "stub checklist: all OK"\nexit 0\n' > "$REPO/image/verify-contents.sh"
}

field() { printf '%s\n' "$ROW" | cut -f"$1"; }

run_gate() {
    run "$REPO/ci/bin/qci" image --no-boot
    ROW=$(awk -F'\t' '$1=="image" && $2=="verify-contents"' "$T"/runs/*/results.tsv)
    echo "status=$status row=$ROW" >&2
}

@test "image gate: a bundle built from a non-ancestor commit is BLOCKED naming both SHAs, not a fail" {
    stamp "SOURCE qdistro $SIDE clean"
    run_gate
    [ "$status" -eq 0 ]
    [ "$(field 3)" = blocked ]
    [[ "$ROW" == *"not an ancestor"* ]]
    [[ "$ROW" == *"$SIDE"* ]]
    [[ "$ROW" == *"$HEAD_SHA"* ]]
    [[ "$ROW" == *"image/build-in-vm.sh"* ]]
    [[ "$ROW" == *"QDISTRO_BUILD_DIR"* ]]
    grep -qx "image_source_relation=not-ancestor" "$T"/runs/*/manifest.txt
    # The checker did not run against the unrelated tree.
    [ ! -e "$T"/runs/*/host/image-verify-contents.log ]
}

@test "image gate: a SHA absent from the local history is BLOCKED as unknown (fetch), not as proven unrelated" {
    stamp "SOURCE qdistro 0123456789abcdef0123456789abcdef01234567 clean"
    run_gate
    [ "$status" -eq 0 ]
    [ "$(field 3)" = blocked ]
    [[ "$ROW" == *"image source unknown"* ]]
    [[ "$ROW" == *"0123456789abcdef0123456789abcdef01234567"* ]]
    [[ "$ROW" == *"$HEAD_SHA"* ]]
    [[ "$ROW" == *"fetch"* ]]
    [[ "$ROW" != *"not an ancestor"* ]]
    grep -qx "image_source_relation=unknown" "$T"/runs/*/manifest.txt
}

@test "image gate: a shallow clone with both commits present reports unknown ancestry (deepen), not not-ancestor" {
    # Fresh history: base (tagged) -> mid -> head, cloned at depth 1, then the
    # base commit fetched on its own. Both objects exist, but HEAD's shallow
    # boundary hides the path between them.
    git -C "$REPO" tag base "$OLD"
    local SH="$T/shallow"
    git clone -q --depth 1 "file://$REPO" "$SH"
    git -C "$SH" fetch -q --depth 1 origin tag base
    git -C "$SH" cat-file -e "$OLD^{commit}"          # the object IS present
    [ "$(git -C "$SH" rev-parse --is-shallow-repository)" = true ]
    ! git -C "$SH" merge-base --is-ancestor "$OLD" HEAD   # git cannot see the path
    stamp "SOURCE qdistro $OLD clean"
    run "$SH/ci/bin/qci" image --no-boot
    ROW=$(awk -F'\t' '$1=="image" && $2=="verify-contents"' "$T"/runs/*/results.tsv)
    [ "$status" -eq 0 ]
    [ "$(field 3)" = blocked ]
    [[ "$ROW" == *"image source unknown"* ]]
    [[ "$ROW" == *"shallow"* ]]
    [[ "$ROW" == *"--unshallow"* ]]
    [[ "$ROW" != *"not an ancestor"* ]]
    grep -qx "image_source_relation=unknown" "$T"/runs/*/manifest.txt
}

@test "image gate: a git error in the ancestry check is BLOCKED as unknown with git's status, not not-ancestor" {
    stamp "SOURCE qdistro $OLD clean"
    mkdir -p "$T/shim"
    local real_git; real_git=$(command -v git)
    cat > "$T/shim/git" <<EOF
#!/bin/bash
for a in "\$@"; do [ "\$a" = merge-base ] && { echo "fatal: simulated object store error" >&2; exit 128; }; done
exec "$real_git" "\$@"
EOF
    chmod +x "$T/shim/git"
    PATH="$T/shim:$PATH" run_gate
    [ "$status" -eq 0 ]
    [ "$(field 3)" = blocked ]
    [[ "$ROW" == *"rc=128"* ]]
    [[ "$ROW" == *"simulated object store error"* ]]
    [[ "$ROW" != *"not an ancestor"* ]]
}

@test "image gate: the recognised pre-monorepo five-repo schema is BLOCKED even though its qdistro SHA is an ancestor" {
    # The real 2026-09-10 bundle: its qdistro SHA IS in the monorepo history,
    # so ancestry alone would have let it through to a fail.
    stamp "SOURCE qdistro $OLD clean" \
          "SOURCE qdwin 815c2876e58a7b1668c0e03a78bdc3e3d62ebf42 clean" \
          "SOURCE qdshell 43e4574511105ae188c961aca2380db8184d5286 clean" \
          "SOURCE qdgreeter 998b4abcba1ccfcc1ba234b22f93b906d036b231 clean" \
          "SOURCE qdlocker 039c14a3be1fbd65a74a625cfb04fc1bd83f9137 DIRTY diff-sha256=0123456789abcdef untracked=2"
    run_gate
    [ "$status" -eq 0 ]
    [ "$(field 3)" = blocked ]
    [[ "$ROW" == *"pre-monorepo sibling-layout"* ]]
    [[ "$ROW" == *"$OLD"* ]]
    grep -qx "image_source_relation=legacy-layout" "$T"/runs/*/manifest.txt
}

@test "image gate: a partial legacy-looking stamp (3 SOURCE lines) is INVALID provenance: fail/20, not blocked" {
    stub_checker_pass
    stamp "SOURCE qdistro $HEAD_SHA clean" \
          "SOURCE qdwin 815c2876e58a7b1668c0e03a78bdc3e3d62ebf42 clean" \
          "SOURCE qdshell 43e4574511105ae188c961aca2380db8184d5286 clean"
    run_gate
    [ "$status" -eq 20 ]
    [ "$(field 3)" = fail ]
    [[ "$ROW" == *"image provenance invalid"* ]]
    [[ "$ROW" == *"3 SOURCE lines"* ]]
}

@test "image gate: a five-repo legacy stamp with a NUL byte in one line FAILS (20), not legacy-blocked (astra r2)" {
    stub_checker_pass
    stamp "SOURCE qdistro $OLD clean" \
          "SOURCE qdwin 815c2876e58a7b1668c0e03a78bdc3e3d62ebf42 clean" \
          "SOURCE qdshell 43e4574511105ae188c961aca2380db8184d5286 clean" \
          "SOURCE qdgreeter 998b4abcba1ccfcc1ba234b22f93b906d036b231 clean"
    # A REAL NUL byte: `clean\0 extra` is outside the writer's grammar, but
    # GNU grep's binary mode used to let every anchored count match.
    printf 'SOURCE qdlocker 039c14a3be1fbd65a74a625cfb04fc1bd83f9137 clean\000 extra\n' \
        >> "$BUILD/extracted/etc/qdistro/release"
    [ "$(tr -dc '\000' < "$BUILD/extracted/etc/qdistro/release" | wc -c)" -eq 1 ]
    run_gate
    [ "$status" -eq 20 ]
    [ "$(field 3)" = fail ]
    [[ "$ROW" == *"image provenance invalid"* ]]
    [[ "$ROW" == *"NUL"* ]]
    [[ "$ROW" != *"sibling-layout"* ]]
    ! grep -q "image_source_relation=legacy-layout" "$T"/runs/*/manifest.txt
    grep -q "stub checklist" "$T"/runs/*/host/image-verify-contents.log
    [ "$(field 7)" = host/image-verify-contents.log ]
}

@test "image gate: a single SOURCE line naming HEAD with a NUL byte FAILS (20)" {
    stub_checker_pass
    stamp
    printf 'SOURCE qdistro %s clean\000\n' "$HEAD_SHA" >> "$BUILD/extracted/etc/qdistro/release"
    run_gate
    [ "$status" -eq 20 ]
    [ "$(field 3)" = fail ]
    [[ "$ROW" == *"NUL"* ]]
    grep -q "stub checklist" "$T"/runs/*/host/image-verify-contents.log
}

@test "image gate: a missing /etc/qdistro/release FAILS (20) with the checklist evidence kept" {
    stub_checker_pass
    run_gate
    [ "$status" -eq 20 ]
    [ "$(field 3)" = fail ]
    [[ "$ROW" == *"image provenance invalid"* ]]
    [[ "$ROW" == *"missing or unreadable"* ]]
    [[ "$ROW" == *"passed but provenance is invalid"* ]]
    grep -q "stub checklist" "$T"/runs/*/host/image-verify-contents.log
}

@test "image gate: a malformed SOURCE line naming HEAD ('clean extra') FAILS (20)" {
    stub_checker_pass
    stamp "SOURCE qdistro $HEAD_SHA clean extra"
    run_gate
    [ "$status" -eq 20 ]
    [ "$(field 3)" = fail ]
    [[ "$ROW" == *"image provenance invalid"* ]]
}

@test "image gate: a duplicated SOURCE line naming HEAD FAILS (20)" {
    stub_checker_pass
    stamp "SOURCE qdistro $HEAD_SHA clean" "SOURCE qdistro $HEAD_SHA clean"
    run_gate
    [ "$status" -eq 20 ]
    [ "$(field 3)" = fail ]
    [[ "$ROW" == *"image provenance invalid"* ]]
    [[ "$ROW" == *"2 SOURCE lines"* ]]
}

@test "image gate: DIRTY metadata outside the writer's grammar FAILS (20); valid DIRTY does not" {
    stub_checker_pass
    local bad
    for bad in "DIRTY garbage" "DIRTY " "DIRTY diff-sha256=0123456789ABCDEF untracked=0" \
               "DIRTY diff-sha256=0123456789abcdef untracked=x" \
               "DIRTY diff-sha256=0123456789abcdef untracked=0 extra"; do
        rm -rf "$T/runs"
        stamp "SOURCE qdistro $HEAD_SHA $bad"
        run_gate
        [ "$status" -eq 20 ] || { echo "accepted: $bad" >&2; return 1; }
        [[ "$ROW" == *"image provenance invalid"* ]]
    done
}

@test "image gate: a symlinked /etc/qdistro/release FAILS (20); it would resolve against the host" {
    stub_checker_pass
    printf 'SOURCE qdistro %s clean\n' "$HEAD_SHA" > "$T/elsewhere"
    ln -s "$T/elsewhere" "$BUILD/extracted/etc/qdistro/release"
    run_gate
    [ "$status" -eq 20 ]
    [ "$(field 3)" = fail ]
    [[ "$ROW" == *"symlink"* ]]
}

@test "image gate: an ancestor build runs the checklist and carries the SHA/distance in the row" {
    stamp "SOURCE qdistro $OLD clean"
    run_gate
    # The fake tree fails the checklist: that verdict must still surface.
    [ "$status" -eq 20 ]
    [ "$(field 3)" = fail ]
    [ -s "$(echo "$T"/runs/*/host/image-verify-contents.log)" ]
    [[ "$ROW" == *"ancestor $OLD (clean), 1 commit(s) behind HEAD $HEAD_SHA"* ]]
    grep -qx "image_source_relation=ancestor" "$T"/runs/*/manifest.txt
    grep -qx "image_source_behind=1" "$T"/runs/*/manifest.txt
    [[ "$output" == *"WARNING image built from ancestor $OLD"* ]]
}

@test "image gate: an ancestor build whose checklist passes is a pass with the distance in its notes" {
    stub_checker_pass
    stamp "SOURCE qdistro $OLD DIRTY diff-sha256=0123456789abcdef untracked=3"
    run_gate
    [ "$status" -eq 0 ]
    [ "$(field 3)" = pass ]
    [[ "$ROW" == *"ancestor $OLD (DIRTY), 1 commit(s) behind HEAD $HEAD_SHA"* ]]
}

@test "image gate: the canonical stamp (SOURCE qdistro HEAD clean) is a pass, exact, with no source note" {
    stub_checker_pass
    stamp "SOURCE qdistro $HEAD_SHA clean"
    run_gate
    [ "$status" -eq 0 ]
    [ "$(field 3)" = pass ]
    [ "$(field 8)" = "static checklist passed ($BUILD/extracted)" ]
    grep -qx "image_source_relation=exact" "$T"/runs/*/manifest.txt
    grep -qx "image_source_state=clean" "$T"/runs/*/manifest.txt
}

@test "image gate: the null OID is invalid provenance: fail/20, not a blocked identity" {
    stub_checker_pass
    stamp "SOURCE qdistro 0000000000000000000000000000000000000000 clean"
    run_gate
    [ "$status" -eq 20 ]
    [ "$(field 3)" = fail ]
    [[ "$ROW" == *"image provenance invalid"* ]]
    [[ "$ROW" == *"non-null"* ]]
    grep -q "stub checklist" "$T"/runs/*/host/image-verify-contents.log
}

@test "image gate: a bundle built from HEAD itself (valid DIRTY) runs the checklist and passes when it does" {
    stub_checker_pass
    stamp "SOURCE qdistro $HEAD_SHA DIRTY diff-sha256=0123456789abcdef untracked=0"
    run_gate
    [ "$status" -eq 0 ]
    [ "$(field 3)" = pass ]
    [[ "$ROW" == *"DIRTY"* ]]
    grep -qx "image_source_relation=exact" "$T"/runs/*/manifest.txt
    grep -qx "image_source_state=DIRTY" "$T"/runs/*/manifest.txt
}

@test "image gate: an explicit --root is inspected as given (no source check)" {
    stamp "SOURCE qdistro $SIDE clean"
    run "$REPO/ci/bin/qci" image --no-boot --root "$BUILD/extracted"
    ROW=$(awk -F'\t' '$1=="image" && $2=="verify-contents"' "$T"/runs/*/results.tsv)
    [ "$status" -eq 20 ]
    [ "$(field 3)" = fail ]
    ! grep -q "image_source_relation" "$T"/runs/*/manifest.txt
}
